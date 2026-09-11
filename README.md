# pdf-dispatch

PDF 解析分布式调度系统：调度中心接收 PDF → 分页切片 → 内存队列 → 多个无状态 worker 并行 OCR 解析 → 回调合并 → 返回行记录 JSONL。

## 架构

```
  client ──submit PDF──▶ 调度中心 (:28765)
                            │
        ┌───────────────────┼───────────────────┐
        │ claim (pull)      │ claim (pull)      │ claim (pull)
        ▼                   ▼                   ▼
   worker-1 ◀─ push ─   worker-2 ◀─ push ─   worker-3
   (纯asyncio          (纯asyncio           (纯asyncio
    无端口)              无端口)              无端口)
```

- **调度中心**（scheduler）：FastAPI + uvicorn，端口 28765。内存队列 + **本地文件持久化**（`data/jobs/` `data/tasks/` `data/pdfs/`，无数据库）。
- **worker**：纯 asyncio 事件循环（无端口、无 FastAPI）。自动感知本机 CPU 核数作为并发能力上报；heartbeat 周期上报空闲度；空闲时主动 pull 任务、完成后 push 结果。
- **完全异步**：调度中心全程被动接受请求；worker 主动 pull + push。两者解耦，任意一方重启不影响另一方。

## 核心设计决策

| 选型 | 理由 |
| --- | --- |
| 本地文件持久化（无 DB） | 状态仅几 KB，崩溃扫目录重建队列即可；去掉 MongoDB/MySQL 依赖，部署更简单 |
| Worker pull（非 push） | 调度中心不主动连 worker，worker 在 NAT/防火墙后也能工作 |
| Worker 纯 asyncio 无端口 | worker 不发 HTTP 请求给 scheduler，不需要暴露端口；攻击面更小 |
| CPU 核数自检 | `capacity = os.cpu_count()` 自动感知；无需在 `servers.txt` 手工填并发数 |
| Claim 原子派发 | `deque.popleft()` + 写 task 记录 = 同一分片不会被两个 worker 拿到 |
| 行契约统一 | 合并结果 = pdf2tree `rows.jsonl`（line/table/page），下游 classify/rebuild 直接吃 |

## 接口

| 方法 | 路径 | 请求方 | 说明 |
| --- | --- | --- | --- |
| POST | `/jobs` | 客户端 | multipart 上传 PDF，202 + job_id |
| GET | `/jobs/{id}` | 客户端 | 查状态 |
| GET | `/jobs/{id}/result` | 客户端 | 下载合并后 JSONL |
| POST | `/internal/claim` | worker | 取一片（响应为元数据 + pdf_url，两步式） |
| GET | `/internal/task/{id}/pdf` | worker | 拉取该分片预切 PDF（原始二进制） |
| POST | `/internal/task_done` | worker | 推回 `{ok, records, parse_ms}` |
| POST | `/internal/heartbeat` | worker | 上报 `{backend_id, capacity, active_tasks, pdf_capable}` |

## 行契约

```jsonl
{"t":"page","page":1}
{"t":"line","page":1,"y":74.0,"x":143.0,"yf":0.04,"label":"text","text":"ICS 35.040"}
{"t":"line","page":29,"y":278.5,"x":830.0,"text":"1、为向个人信息主体清"}
{"t":"table","page":29,"y":185.5,"html":"<table>...</table>"}
```

与 pdf2tree `rows.jsonl` 同结构，下游可直接进入 classify → rebuild → govern。

---

## 三种部署模式

### 模式 A：Docker Compose（开发/单机测试）

```bash
docker-compose up --build --scale worker-v6=2
python examples/submit.py http://localhost:28765 doc.pdf
```

### 模式 B：物理机绿色包（生产、目标机零预装零网络）

**前提**：一台能联网的 Linux 构建机（或 WSL2、CI 机器），执行一次。

```bash
cd /path/to/pdf_dispatch

# 0. 一次性：批量配 SSH 密钥（并行，已免密的机器自动跳过）
python scripts/setup_keys.py -p YourPassword     # 所有机器同一密码 -> 并行
python scripts/setup_keys.py -f passwords.txt    # 不同密码：每行 host:password

# 1. 构建 + 打包（约 5 分钟；下载 Python + pip 装依赖 + 下模型 + 出 tarball）
./scripts/build_standalone.sh
```

第 0 步只需跑一次，之后重跑 `deploy.py` 即可热更新代码。

产出：`dist/pdf-distribute-<ver>.tar.gz`

```bash
# 3. 编写目标机列表
cp servers.txt.example servers.txt
vim servers.txt
# 每行：user@app_host app=scheduler|worker
# 示例：
#   admin@10.0.0.10 app=scheduler
#   admin@10.0.0.11 app=worker
#   admin@10.0.0.12 app=worker

# 4. 一键分发
python scripts/deploy.py servers.txt
```

### 模式 C：Docker 单机部署（替代模式 A，逻辑相同）

```bash
docker build -t pdf-dispatch .
docker run -d -p 28765:28765 -v $PWD/data:/data pdf-dispatch \
uvicorn scheduler.main:app --host 0.0.0.0 --port 28765
```

### 模式 D：GitHub Actions 自动构建（目标机零 Linux 构建环境）

如果**没有 Linux 构建机**，用 GitHub Actions 免费 Linux runner 自动构建：

```bash
# 1. 安装 GitHub CLI（Windows: winget install --id GitHub.cli；msys2: pacman -S github-cli）
# 2. 登录：gh auth login
# 3. push 标签触发构建
git tag v0.1.0 && git push origin v0.1.0

# 4. 监控进度：GitHub 仓库 → Actions 标签页
# 5. 构建完成后产物自动发布到 GitHub Releases

# 6. 下载产物
gh release download v0.1.0 --pattern "pdf-distribute-*.tar.gz"
#   解压到 msys2 目录
tar -xzf pdf-distribute-0.1.0.tar.gz -C .

# 7. 部署
python scripts/deploy.py servers.txt
```

完整 workflow 已写在 `.github/workflows/build.yml`——本地零 Linux 也能出产物。

---

## 环境变量

### 调度中心

| 变量 | 默认值 | 说明 |
| --- | --- | --- |
| `DATA_DIR` | `./data` | jobs/tasks/pdfs/results 持久化根目录 |
| `PORT` | 28765 | HTTP 端口 |

### Worker

| 变量 | 默认值 | 说明 |
| --- | --- | --- |
| `SCHEDULER_URL` | http://localhost:28765 | 调度中心地址 |
| `PDF2TREE_PATH` | 空 | 指向 pdf2tree 工程根即启用 OCR+矢量双路；留空退化为纯 pymupdf |
| `OCR_MODEL_DIR` | 空 | 离线 OCR 模型目录（指向 `models/official_models/`） |
| `CAPACITY` | 0（自动=CPU 核数） | 手动覆盖并发能力 |
| `POLL_INTERVAL` | 1.0 | 无任务时 claim 轮询间隔（秒） |
| `HEARTBEAT_INTERVAL` | 10.0 | 心跳上报间隔（秒） |
| `PORT` | 28765 | HTTP 端口 |

---

## 目录结构（绿色包部署后）

```
/opt/pdf-distribute/                ← DEPLOY_DIR，可改
├── venv/                           ← 自带 Python 3.11 + site-packages（paddle/uvicorn/fastapi）
├── models/official_models/         ← 4 套离线 OCR 模型
├── pdf2tree/                       ← 解析引擎（app/core/extract.py 等）
├── scheduler/ worker/ shared/      ← pdf_dispatch 应用代码
├── start_scheduler.sh              ← 调度中心启动（已注入 PADDLE_PDX_CACHE_HOME 等）
├── start_worker.sh                 ← worker 启动
├── data/                           ← 运行时状态（pdfs/jobs/tasks/results）
│   ├── jobs/<job_id>.json
│   ├── tasks/<task_id>.json
│   ├── pdfs/<job_id>.pdf
│   └── results/<job_id>.jsonl
└── logs/worker.log                 ← 运行日志
```

---

## 运行示例

```bash
# 提交 PDF → 拿 job_id
$ curl -F "file=@/path/to/GB/T35273.pdf" http://localhost:28765/jobs
{"job_id": "a1b2c3...", "pages": 40, "chunks": 4}

# 轮询进度
$ curl http://localhost:28765/jobs/a1b2c3...
{"status": "running", "chunks_done": 2, "num_chunks": 4, ...}

# 完成后下载行记录
$ curl http://localhost:28765/jobs/a1b2c3.../result -o result.jsonl
$ wc -l result.jsonl        # 例如 1177 行
```

---

## 安全性

- **deploy.py** 用 SSH 密钥认证（不传密码），密钥通过 `scripts/setup_keys.py` 并行批量抄到目标机；
- `start_worker.sh` / `start_scheduler.sh` 内置 `PADDLE_PDX_CACHE_HOME` 环境变量，PaddleOCR **永不联网下载**；
- 调度中心只监听入站，**不主动发起到 worker 的连接**；
- worker 无端口，无法从外部访问；
- `.data/tasks/*.json` 落盘时走 `write .tmp → os.replace` 原子防写崩溃留半截。

---

## 依赖

```
# requirements.txt
fastapi>=0.104
uvicorn[standard]>=0.24
httpx>=0.25
pydantic>=2.5
pymupdf>=23.11
# paddle 安装在 build_standalone.sh 里完成（按平台选 paddlepaddle / paddlepaddle-gpu）
```

---

## 典型故障排查

| 现象 | 原因 | 排查 |
| --- | --- | --- |
| worker 日志报 `RuntimeError: 离线 OCR 启动失败` | `OCR_MODEL_DIR` 指向目录缺模型文件 | 检查 `models/official_models/<model_name>/inference.pdiparams` 是否存在 |
| `PADDLE_PDX_CACHE_HOME` 设后仍下载 | 该环境变量被其他环境变量覆盖 | 在 start_worker.sh 末尾加 `echo $PADDLE_PDX_CACHE_HOME` 验证 |
| worker 拉不到任务 | `SCHEDULER_URL` 写错 / 调度中心未启动 | `curl http://<scheduler>:28765/internal/claim` 测通 |
| deploy.py 报 `PID 文件存在但进程不存在` | 上次 worker 被 kill -9 没清理 | 部署脚本已处理：PID 文件 + pkill 双重清理 |
</｜DSML｜parameter>

ssh配置密钥：
python scripts/setup_keys.py -p 123.com
实际部署：
python scripts/deploy.py servers.txt
当发生：
```
[OK]      root@192.192.98.104 (worker, 335s)
[started] pid=23951 worker
[OK]      root@192.192.98.103 (worker, 498s)
[started] pid=1230922 worker
[FAILED]  root@192.192.98.106 (worker): scp 失败: Connection timed out during banner exchange
Connection to 192.192.98.106 port 22 timed out
scp: Connection closed
[OK]      root@192.192.98.105 (worker, 569s)
[started] pid=1667498 worker

==> [deploy] 完成：成功 12/13
```
重新部署失败上传的tar包
python scripts/redeploy.py root@192.192.98.106
curl http://192.192.98.86:28765/docs
curl http://192.192.98.86:28765/stats


40页非扫描件：
python examples/submit.py http://192.192.98.86:28765 D:/project/py_agent/PP-OCRv6/std_docs/GBT35273b.pdf
292页扫描件A：
python examples/submit.py http://192.192.98.86:28765 "D:/ouryun/pdf/GBT 28448-2019 信息安全技术网络安全等级保护测评要求.pdf"

292页扫描件A-前12页测试：
python examples/submit.py http://192.192.98.86:28765 "D:/project/pdf_dispatch/test/GBT28448-2019_前12页.pdf"
总耗时 483.61s


## 踩坑记录：paddle OCR 推理占用 GIL 导致 worker 心跳停发（v0.1.50 修复）

**现象**：
- OCR 任务执行期间，`/stats` 的 `backends` 变成空数组（所有 worker "消失"），
  任务结束后又集体出现
- worker 日志在 `paddle OCR 初始化完成` 之后完全静默：没有心跳、没有 claim、
  没有报错（`heartbeat failed` 计数为 0——不是发送失败，是根本没发）
- 任务本身不受影响，照常解析、照常回传结果，只是监控上看不见

**根因**：
CPython 的 GIL 是全进程一把锁。paddle 推理（`predictor.run()` 及其 Python
胶水层）持锁时间极长，会把整个进程的所有线程冻住。v0.1.45 曾把解析从事件循环
挪到 `asyncio.to_thread` 线程池——没用，因为 GIL 是进程级的，换线程绕不开，
主线程（事件循环、心跳协程）照样被饿死。

**修复**（`worker/main.py`）：
解析改为 `ProcessPoolExecutor` 子进程执行。子进程有独立 GIL 和内存空间：
- 父进程心跳/claim 循环全程畅通，OCR 期间 backends 稳定可见
- paddle 崩溃只死子进程，worker 主进程自动继续领活
- 模型在子进程首次任务时加载一次并常驻，后续任务直接复用

**教训**：Python 里调用重 CPU 的 C/C++ 库（paddle/torch/自研 C 扩展）时，
不要假设它释放了 GIL。验证方法很简单——跑任务的同时看进程其他线程是否还有
心跳/日志输出。没有就换多进程（multiprocessing / ProcessPoolExecutor），
这也是 gunicorn、celery、torch DataLoader worker 的通用做法。
