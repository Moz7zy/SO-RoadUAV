"""Protocol-neutral CARLA process lifecycle.

Importing this module never imports the CARLA Python package.  The client is
created lazily only after an explicitly confirmed execution path calls
``wait_until_ready``.
"""

from __future__ import annotations

from dataclasses import dataclass
import os
from pathlib import Path
import signal
import socket
import subprocess
import time
from typing import Any, Callable, Iterable, Tuple

from benchmark_generator.config import ConfigError


ProcessSnapshot = Tuple[int, int, str]


@dataclass(frozen=True)
class CarlaLifecycleSpec:
    carla_root: Path = Path("/opt/Carla")
    carla_executable: Path = Path("/opt/Carla/CarlaUE4.sh")
    host: str = "localhost"
    rpc_port: int = 3000
    graphics_adapter: int = 5
    startup_timeout_seconds: float = 120.0
    shutdown_timeout_seconds: float = 30.0
    map_load_timeout_seconds: float = 30.0
    adopt_existing: bool = True
    allow_adopted_shutdown: bool = False

    @classmethod
    def from_config(
        cls,
        config: dict[str, Any],
        host: str,
        allow_adopted_shutdown: bool,
        map_id: str | None = None,
    ) -> "CarlaLifecycleSpec":
        lifecycle = config["lifecycle"]
        runtime = config.get("runtime", {})
        limits = config.get("limits", {})
        timeout_by_map = limits.get("map_load_timeout_seconds_by_map", {})
        map_timeout = timeout_by_map.get(map_id, limits.get("map_load_timeout_seconds", lifecycle.get("map_load_timeout_seconds", 30.0)))
        carla_root = lifecycle.get("carla_root")
        if carla_root is None:
            carla_root = runtime.get("carla_root", str(cls().carla_root))
        carla_executable = lifecycle.get("carla_executable")
        if carla_executable is None:
            carla_executable = runtime.get("carla_executable", str(Path(carla_root) / "CarlaUE4.sh"))
        return cls(
            # R4 keeps executable paths under ``runtime`` so the protocol
            # contract can keep lifecycle identity focused on ports/process
            # ownership.  Preserve the older lifecycle fields as a fallback.
            carla_root=Path(carla_root),
            carla_executable=Path(carla_executable),
            host=host,
            rpc_port=int(lifecycle["rpc_port"]),
            graphics_adapter=int(lifecycle["graphics_adapter"]),
            startup_timeout_seconds=float(config["limits"]["server_ready_timeout_seconds"]),
            map_load_timeout_seconds=float(map_timeout),
            adopt_existing=bool(lifecycle.get("adopt_existing", True)),
            allow_adopted_shutdown=allow_adopted_shutdown,
        )


def build_server_command(spec: CarlaLifecycleSpec) -> list[str]:
    return [
        str(spec.carla_executable),
        f"-carla-rpc-port={spec.rpc_port}",
        "-RenderOffScreen",
        "-vulkan",
        "-nosound",
        f"-GraphicsAdapter={spec.graphics_adapter}",
    ]


class CarlaServerManager:
    """Own or adopt one precisely identified CARLA server process group."""

    def __init__(
        self,
        spec: CarlaLifecycleSpec,
        log_path: Path,
        *,
        popen_factory: Callable[..., Any] = subprocess.Popen,
        snapshot_factory: Callable[[], Iterable[ProcessSnapshot]] | None = None,
        client_factory: Callable[[str, int], Any] | None = None,
        sleep_fn: Callable[[float], None] = time.sleep,
        monotonic_fn: Callable[[], float] = time.monotonic,
        killpg_fn: Callable[[int, int], None] = os.killpg,
    ) -> None:
        self.spec = spec
        self.log_path = Path(log_path)
        self._popen_factory = popen_factory
        self._snapshot_factory = snapshot_factory or _process_snapshot
        self._client_factory = client_factory
        self._sleep = sleep_fn
        self._monotonic = monotonic_fn
        self._killpg = killpg_fn
        self.process: Any | None = None
        self.process_group_id: int | None = None
        self.adopted = False
        self.client: Any | None = None
        self._log_handle: Any | None = None

    @property
    def command(self) -> list[str]:
        return build_server_command(self.spec)

    def start_or_adopt(self) -> None:
        groups = self._matching_groups()
        if len(groups) > 1:
            raise ConfigError(
                f"multiple CARLA process groups match port {self.spec.rpc_port} and GPU "
                f"{self.spec.graphics_adapter}: {sorted(groups)}"
            )
        if groups:
            if not self.spec.adopt_existing:
                raise ConfigError(
                    f"a matching CARLA process group already exists on port {self.spec.rpc_port}; "
                    "this lifecycle is configured to own a newly started server"
                )
            self.process_group_id = next(iter(groups))
            self.adopted = True
            return
        if _tcp_port_open(self.spec.host, self.spec.rpc_port):
            raise ConfigError(
                f"CARLA port {self.spec.rpc_port} is already occupied by a non-matching process; refusing to start"
            )
        self.log_path.parent.mkdir(parents=True, exist_ok=True)
        self._log_handle = self.log_path.open("ab")
        try:
            self.process = self._popen_factory(
                self.command,
                cwd=str(self.spec.carla_root),
                stdin=subprocess.DEVNULL,
                stdout=self._log_handle,
                stderr=subprocess.STDOUT,
                start_new_session=True,
            )
        except BaseException:
            self._close_log()
            raise
        self.process_group_id = int(self.process.pid)
        self.adopted = False

    def wait_until_ready(self) -> Any:
        deadline = self._monotonic() + self.spec.startup_timeout_seconds
        last_error: BaseException | None = None
        while self._monotonic() < deadline:
            if self.process is not None:
                return_code = self.process.poll()
                if return_code is not None:
                    raise ConfigError(f"CARLA server exited before readiness with code {return_code}")
            try:
                client = self._make_client()
                # Keep readiness probes short.  A CARLA RPC issued before the
                # server accepts clients can hold the call until its timeout,
                # so using the full startup deadline here would prevent retry
                # probes after the server becomes ready.
                client.set_timeout(min(5.0, max(1.0, deadline - self._monotonic())))
                world = client.get_world()
                self.client = client
                return world
            except BaseException as exc:
                last_error = exc
                self._sleep(1.0)
        raise ConfigError(f"CARLA server readiness timeout on {self.spec.host}:{self.spec.rpc_port}: {last_error}")

    def load_map_once(self, map_name: str) -> Any:
        if self.client is None:
            self.wait_until_ready()
        assert self.client is not None
        # Map loading has its own versioned timeout.  Do not silently reuse or
        # extend the server-start deadline here; manifests/breadcrumbs record
        # this exact per-map value.
        deadline = self._monotonic() + self.spec.map_load_timeout_seconds
        last_error: BaseException | None = None
        current: str | None = None
        world: Any | None = None
        while self._monotonic() < deadline:
            try:
                self.client.set_timeout(min(self.spec.map_load_timeout_seconds, max(1.0, deadline - self._monotonic())))
                world = self.client.get_world()
                current = _world_map_name(world)
                break
            except BaseException as exc:
                last_error = exc
                self._sleep(1.0)
        if current == map_name:
            assert world is not None
            return world
        if current is None:
            raise ConfigError(f"CARLA initial map check timed out for {map_name}: {last_error}")
        try:
            self.client.set_timeout(min(self.spec.map_load_timeout_seconds, max(1.0, deadline - self._monotonic())))
            world = self.client.load_world(map_name)
        except BaseException as exc:
            raise ConfigError(f"CARLA initial map load failed for {map_name}: {exc}") from exc
        last_name = None
        while self._monotonic() < deadline:
            try:
                self.client.set_timeout(min(self.spec.map_load_timeout_seconds, max(1.0, deadline - self._monotonic())))
                world = self.client.get_world()
                last_name = _world_map_name(world)
                if last_name == map_name:
                    return world
            except BaseException as exc:
                last_error = exc
            self._sleep(1.0)
        raise ConfigError(f"CARLA map readiness timeout: expected {map_name}, got {last_name}; last_error={last_error}")

    def stop(self) -> None:
        group_id = self.process_group_id
        if group_id is None:
            self._close_log()
            return
        if self.adopted and not self.spec.allow_adopted_shutdown:
            self._close_log()
            raise ConfigError("refusing to stop an adopted CARLA process without explicit lifecycle shutdown confirmation")
        if self._group_exists(group_id):
            self._killpg(group_id, signal.SIGTERM)
        deadline = self._monotonic() + self.spec.shutdown_timeout_seconds
        try:
            while self._monotonic() < deadline:
                self._reap_owned_process()
                if not self._group_exists(group_id):
                    break
                self._sleep(0.5)
        except KeyboardInterrupt:
            pass
        if self._group_exists(group_id):
            self._killpg(group_id, signal.SIGKILL)
            kill_deadline = self._monotonic() + 5.0
            try:
                while self._monotonic() < kill_deadline:
                    self._reap_owned_process()
                    if not self._group_exists(group_id):
                        break
                    self._sleep(0.25)
            except KeyboardInterrupt:
                pass
        self._reap_owned_process()
        if self._group_exists(group_id):
            self._close_log()
            raise ConfigError(f"CARLA process group {group_id} did not exit after termination")
        if _tcp_port_open(self.spec.host, self.spec.rpc_port):
            port_deadline = self._monotonic() + self.spec.shutdown_timeout_seconds
            try:
                while self._monotonic() < port_deadline and _tcp_port_open(self.spec.host, self.spec.rpc_port):
                    self._sleep(0.25)
            except KeyboardInterrupt:
                pass
            if _tcp_port_open(self.spec.host, self.spec.rpc_port):
                self._close_log()
                raise ConfigError(f"CARLA RPC port {self.spec.rpc_port} remains occupied after owned process-group shutdown")
        self._close_log()
        self.process = None
        self.process_group_id = None
        self.client = None

    def owned_process_group_alive(self) -> bool:
        """Return whether the owned/adopted matching CARLA group is present."""

        group_id = self.process_group_id
        return group_id is not None and self._group_exists(group_id)

    def _make_client(self) -> Any:
        if self._client_factory is not None:
            return self._client_factory(self.spec.host, self.spec.rpc_port)
        import carla  # type: ignore

        return carla.Client(self.spec.host, self.spec.rpc_port)

    def _matching_groups(self) -> dict[int, list[ProcessSnapshot]]:
        groups: dict[int, list[ProcessSnapshot]] = {}
        for record in self._snapshot_factory():
            pid, pgid, args = record
            if _matches_server_command(args, self.spec):
                groups.setdefault(int(pgid), []).append((int(pid), int(pgid), args))
        return groups

    def _group_matches(self, group_id: int) -> bool:
        return group_id in self._matching_groups()

    def _group_exists(self, group_id: int) -> bool:
        return any(
            int(pgid) == int(group_id) and not _pid_is_zombie(int(pid))
            for pid, pgid, _ in self._snapshot_factory()
        )

    def _reap_owned_process(self) -> None:
        if self.process is None:
            return
        try:
            self.process.poll()
        except BaseException:
            pass

    def _close_log(self) -> None:
        if self._log_handle is not None:
            self._log_handle.close()
            self._log_handle = None


def _process_snapshot() -> list[ProcessSnapshot]:
    completed = subprocess.run(["ps", "-eo", "pid=,pgid=,args="], capture_output=True, text=True, check=True)
    records: list[ProcessSnapshot] = []
    for line in completed.stdout.splitlines():
        parts = line.strip().split(None, 2)
        if len(parts) != 3:
            continue
        try:
            records.append((int(parts[0]), int(parts[1]), parts[2]))
        except ValueError:
            continue
    return records


def _pid_is_zombie(pid: int) -> bool:
    try:
        status = Path(f"/proc/{int(pid)}/status").read_text(encoding="utf-8", errors="replace")
    except OSError:
        return False
    for line in status.splitlines():
        if line.startswith("State:"):
            return len(line.split()) > 1 and line.split()[1] == "Z"
    return False


def _matches_server_command(args: str, spec: CarlaLifecycleSpec) -> bool:
    has_launcher = str(spec.carla_executable) in args or str(spec.carla_root / "CarlaUE4/Binaries/Linux/CarlaUE4-Linux-Shipping") in args
    return has_launcher and f"-carla-rpc-port={spec.rpc_port}" in args and f"-GraphicsAdapter={spec.graphics_adapter}" in args


def _tcp_port_open(host: str, port: int) -> bool:
    try:
        with socket.create_connection((host, port), timeout=0.5):
            return True
    except OSError:
        return False


def tcp_port_available(host: str, port: int) -> bool:
    """Return whether a TCP port can be bound for a new CARLA lifecycle."""

    if int(port) < 1 or int(port) > 65535:
        return False
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
            probe.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            probe.bind((host, int(port)))
            return True
    except OSError:
        return False


def _world_map_name(world: Any) -> str:
    carla_map = world.get_map()
    return str(carla_map.name).split("/")[-1]
