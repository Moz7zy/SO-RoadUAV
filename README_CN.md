# SO-RoadUAV

[![Dataset: Hugging Face](https://img.shields.io/badge/dataset-Hugging%20Face-yellow)](https://huggingface.co/datasets/Mozzzy/SO-RoadUAV)
[![Code: MIT](https://img.shields.io/badge/code-MIT-blue)](LICENSE)
[![Data: CC BY-NC 4.0](https://img.shields.io/badge/data-CC%20BY--NC%204.0-lightgrey)](LICENSE-DATASET)

SO-RoadUAV 是面向**低空无人机道路巡检**的 CARLA-first 基准，输入为单帧 UAV RGB 图像，
只发布两个监督目标：

- `road_mask_visible` —— 当前渲染帧中 CARLA 语义 `Roads=1 + RoadLines=24` 的道路 mask。
- `lane_polyline_projected` —— 目标走廊内、无 actor 遮挡投影的图像坐标系车道标线 polyline，
  语义为 `projected_in_image_no_occlusion_test`。

本次发布包含 18,624 张图像（train 12,000 / val 1,584 / test 5,040），来自三个 CARLA 地图，
并附带生成这些数据的脚本与一个与模型无关的评分入口。

状态：**released**。代码使用 MIT 许可证；数据集使用 CC BY-NC 4.0
（署名—非商业性使用）。详见 [`LICENSE`](LICENSE) 与
[`LICENSE-DATASET`](LICENSE-DATASET)。

**命名说明。** 本基准在开发过程和历史 artifact 中一直称为 Benchmark-V3（代码标识中为
`benchmark_v3`）。其对外正式名称为 **SO-RoadUAV**，两者指同一份数据集与同一版协议。

## 数据

5.5 GB 数据集包（18,624 张图像）以 tar 归档形式托管在 Hugging Face，
下载脚本会自动下载、校验并解压：

**<https://huggingface.co/datasets/Mozzzy/SO-RoadUAV>**

```bash
pip install -r requirements.txt
python scripts/download_dataset.py --dest dataset
```

仅评测时可加 `--parts test-eval`，只下载约 0.3 GB（道路 mask、lane JSON、
metadata、manifests），无需完整 5.5 GB。归档布局、分段下载与校验方式详见
[`docs/DOWNLOAD.md`](docs/DOWNLOAD.md)。

## 仓库结构

| 路径 | 内容 |
|---|---|
| `dataset/` | 数据集包的下载目标目录（不纳入 git）；见[数据](#数据) |
| `scripts/` | `download_dataset.py`，Hugging Face 下载与校验脚本 |
| `generation/` | CARLA 生成器：计划、采集 worker、地图准备、数据集打包 |
| `evaluation/` | 官方指标与 `score_predictions.py` |
| `docs/` | 协议、下载、生成、评测、基线适配、可复现性、来源说明 |

建议先读 [`docs/DATASET.md`](docs/DATASET.md) 了解标签契约与 manifest 结构。

## 数据概览

| 子集 | 图像数 | Site | Anchor |
|---|---:|---:|---:|
| train | 12,000 | 131 | 250 |
| val | 1,584 | 20 | 33 |
| test | 5,040 | 53 | 126 |

Test 划分上有四个逻辑视图：`core_scene`（5,040）、`core_observation`（1,890）、
`core_axis`（882）、`challenge_l18`（2,268）。

观测空间：高度 20–100 m，俯仰 −90°…−70°，偏航 −15°…+15°，交通密度
0–40 vehicles/lane/km，天气 ∈ {dry, rain-wet, fog}，太阳高度角 −5°…75°。

下载完成后，在 `dataset/` 下校验完整性：

```bash
cd dataset && sha256sum -c SHA256SUMS
```

## 评测

```bash
python evaluation/score_predictions.py \
    --dataset dataset \
    --predictions ./preds \
    --model my_model \
    --out ./scores
```

预测格式：`preds/road/<sample_id>.png`（二值 0/255，1280×720）与
`preds/lane/<sample_id>.json`（`{"instances": [{"points_image": [[x, y], ...]}]}`，
最多 10 个实例）。评分器输出逐样本指标与视图级报告，包含 site-macro 聚合、
95% bootstrap 区间和相对 reference 的 delta。详见
[`docs/EVALUATION.md`](docs/EVALUATION.md)。

## 复现数据

```bash
python generation/generate_dataset.py --out /data/Benchmark-V3 --plan-only
python generation/generate_dataset.py --out /data/Benchmark-V3 \
    --carla-python /path/to/python --carla-root /opt/Carla --gpu 0
```

`--plan-only` 完全离线，可逐字节复现冻结的 18,624 行计划。完整采集需要 CARLA 服务器
以及 `generation/inputs/maps/` 下的三个 OpenDRIVE 地图。详见
[`docs/GENERATION.md`](docs/GENERATION.md) 与
[`docs/REPRODUCIBILITY.md`](docs/REPRODUCIBILITY.md)。

## 基线说明

本仓库**不发布**基线模型代码、权重或结果表。
[`docs/BASELINE_ADAPTATIONS.md`](docs/BASELINE_ADAPTATIONS.md) 记录了在本 benchmark 上
训练六个参考模型（SegFormer-B0、Mask2Former-R50、YOLO26s-semantic、LaneATT-R18、
CondLaneNet-R18、CLRNet-R18）所需的适配。

## 数据可用性

数据集公开托管于 <https://huggingface.co/datasets/Mozzzy/SO-RoadUAV>，采用
CC BY-NC 4.0。生成管线与官方评分器发布于
<https://github.com/Moz7zy/SO-RoadUAV>，采用 MIT 许可证。

## 许可与引用

代码（`generation/`、`evaluation/`、`scripts/`）以 MIT 许可证发布。数据集以
**CC BY-NC 4.0** 发布：允许署名再分发与改编，不允许商业使用。完整声明见
[`LICENSE-DATASET`](LICENSE-DATASET)。

图像由 CARLA 仿真器基于其地图资产渲染。CARLA 项目以 CC-BY（署名 CVC/UAB）
发布其专有资产；这些地图资产仍以 CC-BY 提供，不受上述非商业条款限制——非商业
条款仅覆盖 SO-RoadUAV 的筛选、标注与打包。详见 [`NOTICE`](NOTICE)。

引用格式：

> Wang, Z., Zhao, J., Liu, X., Gao, X., & Ye, K. *SO-RoadUAV: A Scene-Observation
> Diagnostic Benchmark for UAV Traffic-Monitoring Road Perception* (2026).

机器可读元数据见 [`CITATION.cff`](CITATION.cff)。
