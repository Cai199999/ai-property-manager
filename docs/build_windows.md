# Windows 绿色版打包指南

目标：把本项目的 Java 网关（含前端）+ Python 后端打包成一个双击即可运行的目录。
用户只需双击 `start.bat`，脚本在后台拉起 8000/8080，等待就绪后打开浏览器。

## 0. 先回答数据库问题

**本项目目前没有任何数据库连接。**

- `backend/` 中没有 SQLAlchemy、PyMySQL、sqlite3 相关代码，也没有 `DATABASE_URL` 之类的配置。
- 数据来源是 Excel 文件：`backend/.runtime/rpa_downloads/tables/*.xlsx`，配套的
  `upload_index.json` / `manifest.json` / `schema_groups.json` 也是普通 JSON 文件。
- 查询导出的 Excel / Word 报告落在 `backend/.runtime/exports/`。

因此**不存在“从 MySQL 改回 SQLite”这件事**，也没有需要迁移的表结构或连接串。
如果后续确实要引入 SQLite，属于新功能（例如查询历史、用户配置持久化），需要单独设计，
不建议为了打包而先加一个空数据库。

## 1. 打包后的目录结构

```
ai-property-manager/
├── start.bat                     # 双击启动
├── 停止助手.bat                  # 一键停止
├── gateway/
│   └── target/ai-property-gateway-0.1.0.jar   # 内含前端静态资源
├── backend/
│   ├── ai-property-backend.exe   # PyInstaller 产物（也可整体放 dist 目录）
│   ├── .env                      # 必须外置，用户可编辑 Key
│   └── .runtime/                 # 首次运行自动创建（数据/导出/PID）
└── .venv/                        # 仅源码版需要；打包版可删除
```

`start.bat` 的探测顺序：`backend\ai-property-backend.exe` → 根目录 `.venv\Scripts\python.exe`
→ `backend\.venv\Scripts\python.exe`，所以源码版与打包版可以共存。

## 2. 构建 Java 网关（含前端）

需要 JDK 21 + Maven：

```bat
cd /d <项目根目录>
mvn -f gateway\pom.xml clean package -DskipTests
```

产物：`gateway\target\ai-property-gateway-0.1.0.jar`，前端静态资源已经在 jar 内
（`src/main/resources/static/`），无需单独部署前端。

## 3. 构建 Python 后端（PyInstaller）

### 3.1 准备虚拟环境（复用全局依赖的旧约定不适用于绿色包）

```bat
cd /d <项目根目录>
python -m venv .venv
.venv\Scripts\python.exe -m pip install -U pip
.venv\Scripts\python.exe -m pip install -r backend\requirements.txt
.venv\Scripts\python.exe -m pip install pyinstaller
```

> 打包产物必须带齐 pandas / numpy / matplotlib / python-docx / openpyxl / fastapi / uvicorn，
> 否则 exe 启动会缺模块。建议在干净的 venv 里打包。

### 3.2 打包命令

在 **backend 目录**执行（`serve.py` 是统一入口）：

```bat
cd /d <项目根目录>\backend
..\.venv\Scripts\pyinstaller.exe --noconfirm --clean --onedir ^
  --name ai-property-backend ^
  --hidden-import app.sandbox.runner ^
  --hidden-import uvicorn.logging ^
  --hidden-import uvicorn.loops.auto ^
  --hidden-import uvicorn.protocols.http.auto ^
  --hidden-import uvicorn.protocols.websockets.auto ^
  --hidden-import uvicorn.lifespan.on ^
  serve.py
```

参数说明（与“`--add-data` 带上 app 和 .env”的常见做法不同，原因如下）：

| 参数 | 作用 |
|---|---|
| `--onedir` | 推荐目录模式：启动快、杀毒误报少；`--onefile` 每次启动都要解压，慢且更容易被拦截 |
| `--hidden-import app.sandbox.runner` | runner 由 `serve.py` 内部导入，静态分析可能漏掉 |
| `--hidden-import uvicorn.*` | uvicorn 大量动态导入，不显式声明会在 exe 里报 `No module named uvicorn.loops.auto` |
| **不需要 `--add-data "app;app"`** | `serve.py` 导入 `app.main`，PyInstaller 会自动把整个包作为模块打进去；重复 `--add-data` 只会让体积翻倍 |
| **不要 `--add-data ".env;."`** | `.env` 含用户自己的 API Key，必须外置可编辑。打包后把 `backend\.env` 复制到 exe 同目录即可 |

`app/config.py` 已处理冻结环境：检测到 `sys.frozen` 时，应用根目录取
`Path(sys.executable).parent`，也就是 exe 所在目录。因此：

- `.env` 放在 exe 同目录；
- `.runtime/` 自动创建在 exe 同目录；
- 与启动时的工作目录无关。

### 3.3 产出与拷贝

```
backend\dist\ai-property-backend\
├── ai-property-backend.exe
└── _internal\...
```

把整个 `ai-property-backend` 目录改名为 `backend` 放回项目根，或者直接把
`dist\ai-property-backend\*` 拷贝到 `<项目根>\backend\`，并把 `.env` 一起放进去。
（源码版务必备份，避免覆盖 `backend\app`。）

## 4. Windows 上的代码执行沙箱

- Linux 使用 bwrap 命名空间隔离。
- Windows 没有 bwrap，`CodeExecutor` 自动降级为**受限子进程**：
  独立解释器/独立临时工作目录/清空敏感环境变量/超时强杀；
  代码仍只在子进程执行，不会跑进 FastAPI 进程。
- 降级模式的安全边界主要依赖 `code_safety.py` 的 AST 校验（禁止危险导入、
  文件写、网络与反射调用）。该边界弱于 bwrap，仅适用于可信的本地绿色版。
- 降级时接口返回的 `warnings` 会带一条“当前未启用 bwrap 隔离”的提示，便于排查。

## 5. 启动与停止

- 双击 `start.bat`：后台启动后端与网关，等待健康检查通过后打开
  `http://127.0.0.1:8080`。
- 双击 `停止助手.bat`：按 `.runtime\*.pid` 结束进程，并用命令行精确匹配兜底，
  不会误杀机器上其他 Java/Python 进程。

## 6. 运行环境依赖

| 依赖 | 说明 |
|---|---|
| JDK/JRE 21 | 绿色版建议用 `jlink` 生成精简 JRE，并把 `javaw.exe` 一起打包；否则用户机器需装 Java |
| Python | 打包成 exe 后不再需要；源码版需要 Python 3.11+ |
| VC++ 运行库 | PyInstaller 产物依赖，Win10/11 一般自带 |
| 中文字体 | Windows 自带微软雅黑/黑体，Word 报告已适配 `%WINDIR%\Fonts` |

## 7. 常见问题

1. **`javaw` 不是内部或外部命令**：用户机器未安装 Java，或只装了 JRE 未加 PATH。
   绿色版请把 JRE 的 `bin` 目录一起分发，并在 `start.bat` 里改用相对路径 `.\jre\bin\javaw.exe`。
2. **首次查询报“大模型鉴权失败”**：检查 exe 同目录 `.env` 中的 `DEEPSEEK_API_KEY`。
3. **端口被占用**：`start.bat` 使用 8000/8080 固定端口；先运行 `停止助手.bat`，
   或用 `netstat -ano | findstr :8080` 查找占用进程。
4. **杀毒软件拦截**：PyInstaller onefile 产物易被误报，优先用 `--onedir`，必要时对目录加白名单。
5. **打包后中文乱码**：确认 `.env` 保存为 UTF-8；`serve.py` 已设置 `PYTHONIOENCODING=utf-8`。
