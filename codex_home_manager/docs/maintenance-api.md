# 维护中心接口

这些接口只读。原生服务只转发下面四条 GET 路由；没有安装、修复、重启或执行命令接口。全部响应设置 `Cache-Control: no-store`。源码见 `backend/maintenance.py`；目录源为 `packaging/runtime/cockpit-maintenance.json`。

## GET /api/maintenance/status

读取当前服务携带的变更目录对应的证据元数据，以及分页准备和安装回执的白名单字段。不计算证据文件哈希，不扫描原始任务历史。

`source_workspace`：可选查询参数，字符串，最长1024字符。用于指定包含本次修复源码的父工作区绝对路径，而非某个 Codex Home 或应用安装目录。该目录必须包含 `AGENTS.md`、`scripts` 目录和 `codex_home_manager/packaging/runtime/cockpit-maintenance.json`；目录文件必须与当前服务携带的目录逐字节哈希一致。开发环境省略时尝试源码父工作区；打包环境省略时不推测源码位置。空字符串采用省略行为。

返回 `observedAt`、`catalogUpdatedAt`、`catalogSha256`、`sourceWorkspaceAvailable`、`entries`、`receipts`。`entries` 仅含登记条目的ID及证据路径、可用性、字节数和修改时间；缺失或不可读不伪装成通过。`receipts` 区分准备和安装记录，可含受管理安装包的路径、大小和预期哈希。`runtimeVerified` 固定为 `false`：文件存在或回执匹配不证明当前进程采用了修复。

指定目录结构不符合要求返回400；目录指纹不同返回409；查询参数超过长度限制返回422。客户端必须比较 `catalogSha256` 与自己构建时绑定的目录哈希，不一致时不能把导出的计划视为可执行依据。

## GET /api/maintenance/evidence/{entry_id}/{evidence_index}

对目录登记的单个证据文件计算指纹，不返回正文。调用者不能直接传入任意文件路径。

`entry_id`：必填路径参数，字符串。用于指定变更目录中的稳定ID，例如 `desktop-pagination-6511`；使用标题或不存在的ID返回404。

`evidence_index`：必填路径参数，整数。用于指定该条目 `evidence` 数组中的零基索引，不是整个目录的序号。负数或越界返回404；不能解析为整数返回422。目录更新后应重新取得对应索引，不能复用另一目录版本的旧索引。

`source_workspace`：可选查询参数，最长1024字符。用于指定证据所在的源码父工作区，执行与状态接口相同的结构检查及目录哈希匹配；不是证据文件路径。打包环境未指定源码时返回409。

`catalog_sha256`：可选查询参数，字符串，最长64字符。用于指定调用者正在展示的目录SHA-256，将证据索引绑定到该目录版本，大小写不影响比较。维护界面始终传入构建时绑定的目录哈希；服务目录变化时返回409，客户端应刷新目录状态，不能把另一版本中同一索引的摘要放在旧证据下。省略仅为兼容已有调用，不能因此声称前后端目录已一致；超长参数返回422。

返回证据元数据、`observedAt`、`hashStatus` 和 `sha256`，以及绑定字段 `catalogSha256`、`entryId`、`evidenceIndex`、`path`。客户端须将四个绑定字段与正在核验的目标逐项比较，不匹配不得展示成功。文件不超过131072字节且读取前后身份、大小和修改时间一致时，`hashStatus` 为 `complete`，`sha256` 为完整文件摘要。超过预算时为 `size_limit`，摘要为 `null`，不得把前缀摘要当完整摘要。

文件缺失返回404；不可读或路径越界返回403；读取期间变化返回409。此摘要仅绑定本次文件内容，不证明其陈述正确、修复已应用或实际运行安全。

## GET /api/maintenance/runtime

按需复用现有检测器，读取版本与分页记录、浏览器worker检测结果、提供商兼容性及待验收事务状态。不执行待检查Home配置的CLI或脚本，不发送模型请求，不恢复任务，不运行历史修复。无法只靠元数据确定的CLI版本可以为空，不以执行未知程序补齐。

provider缺少可核验的官方目录、文件或合法哈希基线时返回unknown，不把未取得证据当作官方已升级。静态安装路径及文件摘要也不能证明当前进程已经使用该文件。

`codex_home`：可选查询参数，字符串，最长1024字符。用于指定由现有路径解析器处理的 Codex Home；省略时采用服务的默认Home解析规则。它影响Home相关检测，不把机器级Desktop安装或浏览器worker检查变成该Home专属检查。无效Home返回400；参数超长返回422。

返回 `observedAt`、固定为 `false` 的 `runtimeVerified`、`probeStatus` 和 `checks`。每项分别含 `id`、`state`、`reason`、`scope`、`details`；客户端应展示各自检测范围。某一检测器失败或预算不足时，该项为 `unknown`，不会据此宣称全部检查通过。分页项的 `details.receiptObservedAt` 是历史回执时间，`checkedAtMs` 是当前读取时间，`currentRuntimeVerified=false` 和 `evidenceScope=receipt_only` 明确不证明当前生效。读取到待验收锁文件不代表持有writer guard，未找到也不能证明没有其他事务；解析错误与预算不足分开报告。

共享预算为5秒、累计最多1GiB；provider二进制单文件最多512MiB，按64KiB分块计算摘要，不整文件载入内存。配置和清单文本仍采用独立的小文件上限。超时或断连后不启动后续检测；已经进入的操作系统调用不能强制中断，仍保留进程内检测槽位直到后台退出。该槽位同时限制不同Home，重复请求不排队。

`probeStatus=completed` 仅表示本次检测流程完成，不代表检查结果全部健康。`partial`、`partial_budget`、`partial_deadline` 表示未完成；`cancelled` 表示已取消；`in_progress` 表示同一Home仍在检测，`busy` 表示其他Home占用槽位。后两者返回unknown检查，不复用其他Home的结果。客户端应等待明确的本次结果，不以反复刷新累积探测。

## GET /api/maintenance/artifact/ready

只核验准备回执指向的受管理 `.msix` 或 `.msixbundle` 文件。回执必须提供合法SHA-256，解析后的路径必须位于既有制品根目录内。以下参数是目标一致性断言，不是任意文件选择器。

`expected_path`：可选查询参数，字符串，最长4096字符。用于指定状态接口中正在展示的准备包路径；维护界面始终传入。服务解析路径后按平台路径大小写规则比较。若当前准备回执已切换到其他路径，返回409而不把新包的核验结果归到旧包名下；超长参数返回422。

`expected_sha256`：可选查询参数，字符串，最长64字符。用于指定状态接口中该包的预期SHA-256，大小写不影响比较；维护界面与路径一起传入。回执的预期哈希已变化时返回409，要求重新取得状态；超长参数返回422。省略断言参数保留旧调用兼容性，但调用者仍须复核响应的路径和预期哈希归属。

文件不得超过512MiB；按1MiB分块读取，读取预算10秒。前后检查文件身份、大小、修改时间及实际读取字节数；超预算或变化不接受部分摘要。初始体积超限返回413；准备回执无效、文件不可用、超时或读取期间变化返回409。

成功响应包含制品元数据、`actualSha256`、`expectedSha256`、`hashMatches`、`observedAt`；`installed` 和 `runtimeVerified` 固定为 `false`。哈希不匹配仍返回成功的核验结果，但 `hashMatches` 为 `false`，客户端必须拒绝将该制品作为已验证包。哈希相同不等于签名有效，也不替代安装后的真实工具和UI验收。
