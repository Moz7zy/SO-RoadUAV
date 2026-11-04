# Benchmark-V3 数据集

Benchmark-V3 是面向低空道路巡检的 CARLA-first 基准，输入为单帧无人机 RGB 图像。
本目录是冻结版本 `Benchmark-V3-1.0` 的确定性公开序列化。

状态：`released`。

**命名说明。** 本基准在开发过程和历史 artifact 中一直称为 Benchmark-V3（代码标识中为
`benchmark_v3`）。其对外正式名称为 **SO-RoadUAV**，两者指同一份数据集与同一版协议。

**许可。** 本数据集以 **CC BY-NC 4.0**（Attribution-NonCommercial 4.0 International）
发布：允许署名再分发与改编，不允许商业使用。详见 [`LICENSE`](LICENSE)。

## 任务与标签

1. **道路区域分割** —— 目标 `road_mask_visible`，二值 PNG（0 / 255）。
   前景为当前渲染帧中 CARLA 语义 `Roads=1` 与 `RoadLines=24` 的并集。
2. **目标走廊车道线恢复** —— 目标 `lane_polyline_projected`，JSON 文档，
   `instances[*].points_image` 给出图像坐标系下的车道标线 polyline，语义为
   `projected_in_image_no_occlusion_test`。每个 instance 对应一条独立的物理标线。
   评测 ROI 取自 metadata 的 `target_corridor_polygon`；ROI 之外的预测不属于
   目标走廊证据。

本数据集不发布 Visible/Full 双套标签。道路朝向、走廊几何、车道图、拓扑标签和
地图先验只用于切片与 QA。

## 目录结构

```
train/  val/  test/
    rgb/                     JPEG，1280 x 720
    road_mask_visible/       PNG，0 / 255
    lane_polyline_projected/ JSON，每图最多 10 个实例
    metadata/                清洗后的逐样本 metadata
manifests/
    subsets/{train,val,test}.jsonl
    views/{core_scene,core_observation,core_axis,challenge_l18}.jsonl
dataset_card.json
package_manifest.json
LICENSE
SHA256SUMS
```

每个样本只在 `train/`、`val/`、`test/` 之一出现一次。`views/` 下的四个
manifest 是同一批 `test/` 资产的逻辑索引，不复制图像。

**数据入口必须是 manifest。** 不要通过扫描目录自行构造数据集：身份字段、
子集归属和视图成员关系都在 manifest 中。

每条 subset manifest 记录四个相对资产路径（`rgb`、`road_mask`、`lane`、
`metadata`），并重复记录条件摘要、reference 关联、factor 标签和
`label_rule_version`。

## 逻辑视图

四个视图都是 `test/` 的子集。

| 视图 | 图像数 | 含义 |
|---|---:|---|
| `core_scene` | 5,040 | 全部 test 图像，用于场景级汇总 |
| `core_observation` | 1,890 | 每个 anchor 参考条件周围的单因素切片 |
| `core_axis` | 882 | 七类压力轴 |
| `challenge_l18` | 2,268 | 多因素挑战行 |

## 数据构成

| 子集 | 图像数 | Site | Anchor |
|---|---:|---:|---:|
| train | 12,000 | 131 | 250 |
| val | 1,584 | 20 | 33 |
| test | 5,040 | 53 | 126 |

Test 覆盖 53 个 site、126 个 anchor、25 个 leakage group，每个 anchor 40 张
唯一图像（含 reference 别名为 41 个逻辑槽位）。场景包括：直道 1–4 车道、
直道 5–8 车道、弯道 1–4 车道、弯道 5–8 车道、十字路口、匝道。

观测范围：高度 20–100 m，俯仰 −90°…−70°，偏航 −15°…+15°，交通密度
0–40 vehicles/lane/km，三种天气（dry、rain-wet、fog），太阳高度角 −5°…75°。

## 标签规则

发布两个标签规则版本：

| `label_rule_version` | 样本数 | 规则 |
|---|---:|---|
| `lane_marking_projection_default_1.0` | 15,800 | OpenDRIVE 标线投影 + 逐 span 去重 |
| `lane_marking_physical_merge_1.0` | 2,824 | 24 个已确认 site 的跨 span 物理标线合并 |

`metadata.json` 记录每个样本实际使用的规则。车道实例已经是最终结果，下游
不得再合并或拆分。

## 完整性

`SHA256SUMS` 覆盖全部 payload、两份 README、两个 JSON 卡片、`LICENSE` 和七个 manifest。
在本目录执行校验：

```bash
sha256sum -c SHA256SUMS
```

## 引用与许可

许可：**CC BY-NC 4.0**，详见 [`LICENSE`](LICENSE)。允许署名再分发与改编，
不允许商业使用。

引用格式：

> Wang, Z., Zhao, J., Liu, X., Gao, X., & Ye, K. *SO-RoadUAV: A
> Scene-Observation Diagnostic Benchmark for UAV Traffic-Monitoring Road
> Perception* (2026).

图像由 CARLA 仿真器基于其地图资产渲染；这些资产适用的条款请参考 CARLA 项目。
