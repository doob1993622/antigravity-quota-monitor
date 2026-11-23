<div align="center">

<img src="assets/logo.svg" width="84" height="84" alt="Antigravity Model Quota Monitor Logo" />

# Antigravity Model Quota Monitor

**为 Google Antigravity 2.0 打造的实时模型配额监控插件 · 零 Token 消耗**

*Real-time 5-hour & weekly model quota monitor for Google Antigravity — Right Auxiliary Sidebar + Auto-Docking Desktop Widget*

[![Antigravity 2.0](https://img.shields.io/badge/Antigravity-2.0_Plugin-2563eb?style=flat-square&logo=google&logoColor=white)](https://github.com/jibunrock40/antigravity-quota-monitor)
[![Zero Token Cost](https://img.shields.io/badge/Token_Cost-Zero_(Local_RPC)-16a34a?style=flat-square)](https://github.com/jibunrock40/antigravity-quota-monitor)
[![Platforms](https://img.shields.io/badge/Platform-Windows_%C2%B7_macOS_%C2%B7_Linux-475569?style=flat-square)](https://github.com/jibunrock40/antigravity-quota-monitor)
[![License: MIT](https://img.shields.io/badge/License-MIT-f59e0b?style=flat-square)](LICENSE)

[界面预览](#preview) · [双形态特性](#features) · [快速安装](#install) · [快捷操作](#controls) · [工作原理](#architecture) · [English Guide](#english-guide)

</div>

---

<a id="preview"></a>
## ✨ 界面预览（Visual Showcase）

插件同时提供**桌面左下角自适应迷你浮窗**与**右侧辅助边栏面板**两种入口，浅色 / 深色主题自动与客户端或系统同步：

### 1. Windows 桌面迷你置顶浮窗（随 Antigravity 自动启停）
默认停靠在 Antigravity 左侧导航栏底部空白区域；点击迷你卡片即可向右展开全部模型配额与档位明细，也可点击 `钉` 切换为自由拖拽悬浮。

![Windows 桌面自动贴靠浮窗与展开弹窗预览](assets/preview-floating.png)

### 2. 右侧辅助边栏面板（全平台通用）
在对话右上角 **`+`** 菜单中选择 **`模型用量`** 即可打开，开箱即用、无需额外运行环境。

![右侧辅助边栏浅色与深色展开态预览](assets/preview-sidebar.png)

---

<a id="features"></a>
## 🎯 双形态功能对比（Dual Surfaces）

| 功能维度 | 右侧辅助边栏面板 (`quota_panel`) | Windows 桌面迷你浮窗 (`quota_floating`) |
| :--- | :--- | :--- |
| **支持平台** | Windows / macOS / Linux 全平台 | Windows（需 Python 3.9+ & `PyQt5`） |
| **启停方式** | 点击对话右上角 `+` → `模型用量` | **随 Antigravity 自动启动，关闭客户端后自动退出** |
| **当前模型焦点** | 自动识别当前对话 / 最近选用模型，展示 **5h + 一周用量** | 紧凑迷你卡常驻显示当前模型、思考档位及双进度条 |
| **全部模型展开** | 点击卡片一键展开全部分组与档位（`Low` ~ `XHigh`） | 左键点击迷你卡，向右平滑弹出全部模型分组面板 |
| **双视角切换** | 顶部 `已用 / 剩余` 分段按钮切换（本地记忆偏好） | 弹窗与右键菜单支持 `已用视角 / 剩余视角` 实时同步 |
| **智能停靠与避让** | 紧贴客户端右侧辅助栏，自动跟随宿主主题 | 吸附 Antigravity 左下角，客户端最小化/被遮挡时自动隐藏；或切换 `浮` 自由拖拽 |
| **额外 Token 消耗** | **0 Token**（纯本地 Connect-RPC 只读查询） | **0 Token**（复用 `quota_panel` Sidecar 本地 HTTP 缓存） |

---

<a id="install"></a>
## 🚀 快速安装（Installation）

### 方式一：使用 `agy` CLI 一键安装（推荐）

```bash
agy plugin install https://github.com/jibunrock40/antigravity-quota-monitor
```

### 方式二：使用 `git clone` 克隆到插件目录

* **Windows (PowerShell)**：
  ```powershell
  git clone https://github.com/jibunrock40/antigravity-quota-monitor.git "$env:USERPROFILE\.gemini\config\plugins\quota-monitor"
  ```

* **macOS / Linux**：
  ```bash
  git clone https://github.com/jibunrock40/antigravity-quota-monitor.git ~/.gemini/config/plugins/quota-monitor
  ```

### 方式三：手动下载 ZIP 安装

1. 在本仓库页面点击 **Code → Download ZIP** 并解压。
2. 将解压后的目录重命名为 `quota-monitor`，放置到 Antigravity 插件目录（确保 `plugin.json` 直接位于该目录下，**不要多嵌套一层子目录**）：
   * **Windows**：`%USERPROFILE%\.gemini\config\plugins\quota-monitor\`
   * **macOS / Linux**：`~/.gemini/config/plugins/quota-monitor/`

> 安装完成后 Antigravity 会自动发现并加载插件（也可在左侧边栏 **个性化定制 / Customizations** 中确认已启用）。

---

### 🖥️ 开启 Windows 桌面迷你浮窗（可选配置）

**右侧辅助边栏面板无需任何额外依赖**即可在所有平台直接工作；若希望同时启用左下角桌面迷你浮窗，仅需在 Windows 安装 `PyQt5`：

```bash
pip install -r requirements.txt
# 或直接安装：
pip install PyQt5
```

1. 重启 Antigravity（或在「个性化定制」中重新开关一次插件），后台启动器 `launch.mjs` 会自动探测常见 Python / Anaconda / Miniconda / venv 环境中的 `PyQt5` 并拉起浮窗。
2. 如需指定特定的虚拟环境 Python，可设置环境变量 `ANTIGRAVITY_QUOTA_PYTHON` 指向该环境下的 `pythonw.exe` 或 `python.exe`。
3. 未安装 `PyQt5` 或在 macOS / Linux 上运行时，启动器会安静跳过，**完全不影响右侧边栏面板使用**。
4. 如需手动独立调试或拉起浮窗，也可直接运行 `widget\start_widget.vbs`（无黑框静默启动）或 `widget\start_widget.bat`。

---

<a id="controls"></a>
## 🖱️ 快捷操作与交互（Controls）

| 入口 | 操作 | 效果 |
| :--- | :--- | :--- |
| **桌面迷你浮窗** | **左键单击卡片** | 展开 / 收起全部模型组详细用量弹窗 |
| **桌面迷你浮窗** | **点击右上角 `钉 / 浮`** | 在「吸附客户端左下角」与「自由拖拽悬浮」间切换 |
| **桌面迷你浮窗** | **自由模式下按住卡片拖拽** | 任意摆放桌面位置，自动记忆坐标并在同屏安全区域内约束 |
| **展开弹窗** | **`已用视角` / `刷新` / `×` (或 `Esc`)** | 切换已用/剩余显示、强制刷新本地缓存、收起详情弹窗 |
| **浮窗右键 / 托盘图标** | **右键菜单** | 隐藏/显示浮窗、切换停靠模式、切换主题（跟随系统/浅色/深色）、开关「随客户端关闭自动退出」、立即刷新或退出 |
| **右侧边栏面板** | **对话右上角 `+` → `模型用量`** | 打开右侧边栏视图；支持 `已用 / 剩余` 切换、立即刷新及展开全部分组 |

---

<a id="architecture"></a>
## 🧠 工作原理（零 Token 消耗）

```mermaid
flowchart LR
    LS["Antigravity Language Server"] -->|"Read-only RPC: RetrieveUserQuotaSummary & GetUserStatus"| Panel["Sidecar: quota_panel (Node.js)"]
    State["transcript.jsonl & antigravity_state.pbtxt"] -->|"Active Model Detection"| Panel
    Panel -->|"Sidecar Web UI (AUX_PANE)"| Sidebar["Right Auxiliary Sidebar Panel"]
    Panel -->|"runtime.json + localhost /api/quota (10s TTL)"| Widget["Windows Floating Widget (PyQt5)"]
    Launcher["Sidecar: quota_floating (launch.mjs)"] -->|"Auto-start & Parent/Window Lifecycle"| Widget
```

* **零额外额度消耗**：插件不会向任何大模型发起推理请求，只读取 Antigravity 本地 Language Server 的配额汇总接口，并自带 10 秒服务端缓存与 30 秒前后端轮询。
* **纯本地隐私运行**：所有统计均在本机 `127.0.0.1` 回环地址与本地数据目录内完成，不上传任何账号信息、对话内容或配额数据到第三方服务器。

---

## 📁 目录结构（Directory Layout）

```text
quota-monitor/
├── plugin.json                         # Antigravity 插件清单
├── requirements.txt                    # Windows 浮窗可选依赖 (PyQt5>=5.15)
├── LICENSE                             # MIT License
├── assets/
│   ├── logo.svg                        # 插件图标
│   ├── preview-floating.png            # Windows 桌面浮窗预览图
│   └── preview-sidebar.png             # 右侧边栏面板预览图
├── sidecars/
│   ├── quota_panel/                    # 全平台右侧边栏 Web UI Sidecar + 本地 /api/quota 服务
│   │   ├── sidecar.json
│   │   ├── package.json
│   │   ├── main.mjs
│   │   ├── index.html
│   │   ├── styles.css
│   │   └── app.js
│   └── quota_floating/                 # Windows 桌面浮窗跨环境托管启动器
│       ├── sidecar.json
│       ├── package.json
│       └── launch.mjs
└── widget/                             # Windows PyQt5 桌面迷你浮窗与手动启动器
    ├── quota_floating_widget.pyw
    ├── start_widget.vbs
    └── start_widget.bat
```

---

## ❓ 常见问题（FAQ）

1. **右侧边栏的 `+` 菜单里没有出现「模型用量」？**
   请确认 `plugin.json` 直接位于 `~/.gemini/config/plugins/quota-monitor/plugin.json`（解压 ZIP 时注意不要多套一层文件夹），并在左侧「个性化定制」中确保插件处于启用状态。
2. **Windows 桌面浮窗没有自动出现？**
   可在终端运行 `python -c "import PyQt5.QtWidgets; print('OK')"` 验证当前 Python 是否已安装 `PyQt5`。若 `PyQt5` 安装在特定 Conda / venv 中，请将 `ANTIGRAVITY_QUOTA_PYTHON` 环境变量设为对应的 `pythonw.exe` 路径；也可查看 `%USERPROFILE%\.gemini\antigravity\sidecar_data\quota-monitor\quota_panel\data\widget.log` 排查启动记录。
3. **关闭 Antigravity 后浮窗会不会残留在桌面？**
   默认启用了「随 Antigravity 关闭自动退出」：浮窗会监听父进程与主窗口生命周期，客户端退出几秒内自动清理托盘与进程（最小化客户端窗口只会自动隐藏浮窗，不会误退出）。

---

<a id="english-guide"></a>
## 🌍 English Guide

### Overview

**Antigravity Model Quota Monitor** is a local, zero-token-cost quota dashboard plugin for **Google Antigravity 2.0**. It combines a cross-platform **Right Auxiliary Sidebar Panel** with an optional **Windows Always-On-Top Auto-Docking Desktop Widget**, allowing you to check the **5-hour** and **weekly** usage of your active model—and expand all model groups—at a glance.

### Key Highlights

* **Cross-Platform Sidebar Panel (`quota_panel`)**: Works out-of-the-box on Windows, macOS, and Linux via the conversation `+` → **`模型用量`** menu. Automatically detects your active conversation model, displays 5h & weekly progress bars with reset countdowns, and expands to all model groups (`Gemini Models`, `Claude and GPT models`, `Gemini 4 Argon`, etc.) with thinking-tier badges.
* **Windows Floating Mini Widget (`quota_floating`, Optional)**: Launches automatically with Antigravity and exits cleanly when Antigravity closes. Docks snugly into Antigravity's empty bottom-left sidebar area (hiding automatically when minimized or covered), or switches to draggable floating mode (`浮`).
* **Used / Remaining Toggle & Theme Sync**: Switch freely between used and remaining quota perspectives and Light / Dark themes.
* **Strictly Local & Zero Token Cost**: Queries local Language Server Connect-RPC endpoints (`RetrieveUserQuotaSummary` & `GetUserStatus`) over `127.0.0.1` with a 10s cache—consuming **0 LLM tokens**.

### Quick Install

```bash
# Option 1: Antigravity CLI (Recommended)
agy plugin install https://github.com/jibunrock40/antigravity-quota-monitor

# Option 2: Git Clone (macOS / Linux)
git clone https://github.com/jibunrock40/antigravity-quota-monitor.git ~/.gemini/config/plugins/quota-monitor
```

```powershell
# Option 2: Git Clone (Windows PowerShell)
git clone https://github.com/jibunrock40/antigravity-quota-monitor.git "$env:USERPROFILE\.gemini\config\plugins\quota-monitor"
```

* **Option 3 (ZIP)**: Download the repository archive, rename the extracted folder to `quota-monitor`, and place it under `~/.gemini/config/plugins/quota-monitor/` (Windows: `%USERPROFILE%\.gemini\config\plugins\quota-monitor\`).

#### Enable the Optional Windows Floating Widget

The right-sidebar panel requires zero extra dependencies. To also enable the Windows desktop mini widget:

```bash
pip install -r requirements.txt
# or: pip install PyQt5
```

Restart Antigravity (or toggle the plugin once in Customizations). If your `PyQt5` lives inside a custom virtual environment, point `ANTIGRAVITY_QUOTA_PYTHON` to its `pythonw.exe`. On macOS/Linux or systems without `PyQt5`, `launch.mjs` exits cleanly with code `0` so the sidebar panel remains completely unaffected.

### License

Released under the [MIT License](LICENSE).
