# RandomImageAPI

一个基于 FastAPI 和 SQLite 的轻量图床/随机图片 API。图片仍保存在磁盘上；SQLite 管理图片索引、SHA-256、访问统计、目录权重、IP 名单和限流状态。

## 权重规则

随机抽取分两步进行：

1. 按权重选择一个含图目录。
2. 在选中的目录内均匀选择一张图片。

例如目录 A 的权重为 `2`、目录 B 为 `1`，则命中 A/B 的概率约为 `2:1`，与各目录图片数量无关。未设置权重的目录继承最近祖先的配置；没有可继承配置时使用 `1`。子目录的显式配置会覆盖继承值，不会与祖先权重相乘。

使用管理命令可直接预览每个目录和单张图片的理论概率：

```bash
uv run python manage.py weight list
uv run python manage.py weight set /path/to/images/cats 2
uv run python manage.py weight del /path/to/images/cats
```

## 快速开始

需要 Python 3.12+ 和 [uv](https://docs.astral.sh/uv/)。

```bash
git clone https://github.com/Kevin-2106/RandomImageAPI.git
cd RandomImageAPI
cp config.example.toml config.toml
# 修改图片目录与 base_url
uv sync
uv run python manage.py scan
uv run start
```

生产环境建议通过环境变量设置管理密钥：

```bash
export RANDOM_IMAGE_ADMIN_KEY='replace-with-a-long-random-value'
```

也可以用 `RANDOM_IMAGE_CONFIG` 指向其他配置文件，或用 `RANDOM_IMAGE_DATABASE` 单独覆盖数据库路径。默认数据库位于 `data/imageapi.db`，运行时数据库和本机配置均不会提交到 Git。

## API

- `GET /random`：直接返回随机图片
- `GET /random?json=true`：返回图片元数据和永久链接
- `GET /random?latest=true`：从 `latest_dir` 均匀抽取
- `GET /getImage/{sha256}`：按内容哈希获取图片
- `GET /stats`：图库统计
- `GET /docs`：交互式 OpenAPI 文档

管理接口均需 `X-Admin-Key` 请求头：

- `POST /admin/scan`
- `POST /admin/compute-hashes`
- `GET|PUT|DELETE /admin/folder-weights`
- `GET|POST|DELETE /admin/whitelist`
- `GET|POST|DELETE /admin/blacklist`
- `GET /admin/rate-limits`

`GET /admin/folder-weights` 同时返回权重规则与当前图库的概率预览。

## 管理命令

```bash
uv run python manage.py help
uv run python manage.py scan
uv run python manage.py stats
uv run python manage.py hash-compute
```

## 数据库

SQLite 使用 WAL、5 秒 busy timeout 和外键检查。启动或执行管理命令时会自动建表，并兼容迁移早期数据库中的 `sha256` 字段和错误的唯一索引。重复内容允许共享相同 SHA-256。

备份时建议同时停止服务，或使用 SQLite 的在线备份命令，不要只复制正在写入的 `.db` 文件。
