# Codex Home Manager

[中文](#中文) | [English](#english)

## 中文

Codex Home Manager 是一个面向本机 Codex Home 的开源管理工具，提供现代化 Web UI、REST/OpenAPI 和 MCP 接口。它可以检查和管理线程、项目、资源、插件状态、备份、导入导出与离线修复流程。

- 在线入口：[codex-home-manager.simplezion.com](https://codex-home-manager.simplezion.com/)
- Windows 发行版：[GitHub Releases](https://github.com/SimpleZion/codex-home-manager/releases/latest)
- 完整源码：默认 `source` 分支
- 部署与发行产物：`main` 分支

### 主要能力

- 浏览全部主线程和子 agent 线程，按项目、状态、存储、Token 与更新时间筛选和排序。
- 查看线程详情、子线程汇总、每日 Token 时间线、日志和纯净用户 Prompt。
- 导出 Prompt，可选择是否包含元信息、附件上下文、自动化任务、跨线程委派和空行。
- 预览后执行线程显示、隐藏、归档、复制、迁移、瘦身、备份与恢复。
- 管理 `AGENTS.md`、memory、skills、插件缓存和其他 Codex Home 资源。
- 从其他 Codex Home 导入线程、项目和资源。
- 运行只读体检，并生成可直接交给 Codex 的修复 Prompt。
- 通过 REST/OpenAPI 与 MCP 为其他 agent 提供同等能力。

### 安全模型

- 本机连接器只监听 loopback，线上页面不能直接读取任意本机文件。
- 浏览器文件夹模式由用户主动选择目录，并保持只读。
- 写操作需要短期本机授权、状态绑定的预览票据和运行中 Codex 风险确认。
- 备份可选；需要备份时默认使用受控备份根目录。
- 修改 SQLite、全局状态、插件配置或工作区绑定的离线修复必须在 Codex 完全退出后执行。
- 发布产物使用内容寻址文件名、SHA-256、Ed25519 分离签名和独立公钥指纹校验。

### 本地开发

要求 Node.js 22 或更高版本、Python 3.12 和 PowerShell 7。

```powershell
npm ci
python -m pip install --require-hashes --only-binary=:all: -r packaging/windows/requirements-connector.txt
npm run serve
```

另开一个终端启动前端：

```powershell
npm run dev
```

访问 `http://127.0.0.1:5173/`。完整质量门：

```powershell
npm run gate
```

构建 Windows 单文件连接器：

```powershell
pwsh -NoProfile -File scripts/package-local-connector.ps1
```

发布脚本会执行完整测试、两次隔离构建、字节级可复现性比较、黑盒安全验证和公开边界验证。

## English

Codex Home Manager is an open-source local management console for Codex Home. It provides a modern Web UI, REST/OpenAPI endpoints, and MCP tools for threads, projects, resources, diagnostics, backups, import/export, and guarded offline repair.

- Hosted console: [codex-home-manager.simplezion.com](https://codex-home-manager.simplezion.com/)
- Windows releases: [GitHub Releases](https://github.com/SimpleZion/codex-home-manager/releases/latest)
- Complete source: default `source` branch
- Deployed static site and release artifacts: `main` branch

The local connector is loopback-only. Browser folder mode is user-selected and read-only. State-changing operations require short-lived local authorization and state-bound previews. Release artifacts are content-addressed and verified with SHA-256 plus detached Ed25519 signatures.

See [CONTRIBUTING.md](CONTRIBUTING.md) for development requirements and [SECURITY.md](SECURITY.md) for vulnerability reporting.

## License

MIT. See [LICENSE](LICENSE).
