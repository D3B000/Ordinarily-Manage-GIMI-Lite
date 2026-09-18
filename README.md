# Ordinarily Manage GIMI · Lite

基于 PySide6 的 Windows 桌面客户端，用于简单地管理与更新 GIMI。应用本身不提供 GIMI 本体和模组。

> 这玩意儿全部都是用 AI 搓的。Lite 版是因为之前搓了个带背景的版本，体积大也不太实用，就精简了一下。

## 首次使用

前往 **设置 → 路径 → 路径配置**，添加你的游戏目录与 GIMI 目录。

## 功能

- **悬浮面板**：提供加载模组、更新 GIMI 版本等功能。
- **便捷构建**：提供下载源码、一键构建、构建优化、文件优化等功能。
  - 注：构建需要本机具备编译环境（VS2022「使用 C++ 的桌面开发」工作负载，含 MSVC v143 与 Windows SDK），占用较大（约 6–10 GB），官网地址：https://visualstudio.microsoft.com/zh-hans/visual-cpp-build-tools/。
  - 文件优化：对构建产物进行体积与特征层面的处理；相关源码不在本仓库，仅有编译产物随包分发。
- **资源浏览**：提供简易的模组罗列、禁用 / 启用切换、切换键（swapkey）查看、差分变量写回等功能。

## 目录结构

```
OMGLite/
├── main.py                      # 入口
├── OMG.spec / build_pyinstaller.ps1   # 打包（PyInstaller）
├── pyproject.toml               # 依赖与构建配置
├── src/omg/
│   ├── app.py                   # 应用启动
│   ├── core/                    # 核心：构建流水线、配置、路径、仪式加载器、更新、注入编排
│   ├── domain/                  # 领域模块
│   │   ├── inject/              # 3DMigoto DLL 注入 / 进程跟踪 / 注入探针
│   │   ├── launch/              # 启动控制
│   │   ├── media/               # 媒体抽取
│   │   ├── modtools/            # 模组工具（纹理缩放、模组整理）
│   │   └── setting/             # 设置数据层
│   ├── pages/                   # 页面：home / resources / quick_build / setting
│   ├── ui/                      # 界面组件、主题、托盘、浮层
│   ├── widgets/                 # 自定义控件（进度环等）
│   └── resources/               # 随包资源（二进制、图标、QSS、3DMigoto Loader 等）
├── tests/                       # 冒烟测试
└── docs/                        # 设计文档
```

## 开发与运行

需要 Windows + Python 3.11。

```powershell
pip install -e .
python -m omg
```

打包为可执行文件：

```powershell
.\build_pyinstaller.ps1
```

## 许可

本项目采用 **MIT** 许可，见 [LICENSE](LICENSE)。
