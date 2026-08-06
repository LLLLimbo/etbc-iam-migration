# ETBC → IAM 租户数据迁移

本目录提供可恢复、可审计的 ETBC 租户数据迁移工具。当前版本只接受且只迁移以下模块：

- `TENANT`
- `ORGANIZATION`
- `STAFF`

任何缺失、额外、未知或尚未实现的模块都会在本地 fail-closed。IAM 只能通过
`POST /inter/iam/mgmt/migration/v1/batches/import` 写入；工具不会写 IAM 数据库。
`jobTitle`、生日、常用地点、队列和外部账号 ID 等当前无业务目标的员工字段只进入 IAM 的受控
迁移审计，不会据此创建岗位或其他未启用模块数据。

## 运行模型

`preflight` 在只读一致性快照中读取并校验 ETBC，不调用 IAM。`migrate` 将完整固定负载先写入
SQLite，再关闭 ETBC 事务并分片调用 IAM。`resume` 只继续非终态实体；以同一批次再次执行
`migrate` 会加载原快照并重放，以验证 IAM 的幂等结果。`report` 与 `verify` 是同义的本地台账汇总命令。

`web` 提供面向交付工程师的轻量迁移控制台。它通过受控子进程调用同一个 CLI，不复制迁移逻辑；
页面只显示批次元数据、聚合状态、调用耗时和错误码，不显示实体明细或请求负载。Web 操作仍使用
相同的 SQLite 状态、退出码、校验、重试和幂等规则。

同一个 `migrationBatchId` 的 `legacyTenantId`、`enabledModules`、`sourceTimezone` 和
`snapshotAt` 一经保存不可修改。`correlationId` 由批次、实体类型和源 ID 确定性生成。

状态与报告可能包含姓名、证件号、联系方式等个人信息：状态目录会被强制设为 `0700`，SQLite
与报告文件会被设为 `0600`，并拒绝使用符号链接作为状态目录或数据库文件。该目录必须位于受控、
加密且已备份的存储上。保留期结束后，应先确认准确的批次状态目录，再按组织的数据销毁流程同时
清除 `migration-state.sqlite3` 和 `reports/`；仅删除报告并不能清除 SQLite 中的固定迁移负载。

## 配置和秘密

复制并编辑 `config.example.toml`。TOML 只允许保存非秘密设置；任何名称包含 password、token、
secret、credential 或 loginPwd 的键都会被拒绝。当前唯一由进程环境读取的秘密是：

- `ETBC_PASSWORD`

不要将秘密写入命令行值、TOML、`.env`、日志或 shell 历史。下列示例中的 `--env NAME` 只把调用
环境中已经安全注入的变量传入容器，不在命令行中展开变量值。生产 IAM URL 默认必须为 HTTPS；
`allow_insecure_http = true` 仅供隔离集成测试使用。

当前 Java 契约已移除迁移端点的路由级 Token 认证，Python 不要求
`IAM_MIGRATION_INTERNAL_AUTH_TOKEN`，也不发送 `X-Iam-Internal-Token` 或
`X-Iam-Internal-Caller`。因此该内部接口必须通过网络隔离、入口访问控制和变更窗口管理限制访问，
不得暴露到不受信任网络。

生产环境仍建议使用只有目标 schema `SELECT` 权限的账号，但工具不再检查或拒绝账号已有的其他
授权。所有源数据读取仍在 `REPEATABLE READ`、`WITH CONSISTENT SNAPSHOT, READ ONLY` 事务中执行，
并且查询明确列出字段。工具不会查询 `loginPwd`、`loginPwdEncrypt` 或 `sys_user_orgnization`。

部分旧版 ETBC schema 没有仅用于迁移审计的 `biz_participant.iam_lessee_id`、
`sys_orgnization.userId`，也没有组织软删除列 `sys_orgnization.deleted`。工具会在同一个只读事务中
通过 `INFORMATION_SCHEMA.COLUMNS` 检测这三个列：缺少审计列时传递 `null`；缺少 `deleted` 时将
该租户范围内的组织全部按未删除处理。除此之外的必需源字段仍然 fail-closed，不做静默兼容。
数据库与运行节点的 UTC 时钟允许最多 5 秒偏差；超过该范围的未来 `snapshotAt` 仍会被拒绝。

## 构建

从本仓库根目录执行：

```bash
docker build --tag etbc-iam-migration:1.0.0 .
```

`requirements.lock` 对 Python 3.12 镜像中的直接及传递依赖使用精确版本锁定；Linux 生产镜像
仍通过 Dockerfile 构建，不依赖宿主机 Python 包。

### Windows 单文件程序

仓库的 `Build Windows executable` GitHub Actions 工作流会在 Windows Server 2022 x64 Runner 上
使用 Python 3.12.11 和锁定的 PyInstaller 依赖运行单元测试，然后生成：

- `etbc-iam-migrate.exe`
- `etbc-iam-migrate.exe.sha256`

推送到 `main` 或在 GitHub Actions 页面手动运行该工作流即可构建。产物保存在
`etbc-iam-migration-windows-x64` artifact 中，保留 30 天。目标 Windows 机器无需另行安装 Python；
配置文件、`ETBC_PASSWORD` 环境变量、SQLite 状态和报告仍保留在程序外部。

PowerShell 示例：

```powershell
$env:ETBC_PASSWORD = '<由秘密管理系统注入>'
.\etbc-iam-migrate.exe `
  --config C:\migration\config.toml `
  preflight `
  --legacy-tenant-id '<legacy-tenant-id>' `
  --enabled-modules TENANT,ORGANIZATION,STAFF `
  --source-timezone Asia/Shanghai
```

下面的生产示例假定已由安全的运行平台注入 `ETBC_PASSWORD`，并设置这些非秘密运行变量：

```bash
export MIGRATION_CONFIG=/absolute/path/to/config.toml
export MIGRATION_STATE_DIR=/absolute/path/to/restricted-state
export LEGACY_TENANT_ID='<legacy-tenant-id>'
export MIGRATION_BATCH_ID='<globally-unique-batch-id>'
export SNAPSHOT_AT='<UTC-ISO-8601-instant>'
```

`MIGRATION_STATE_DIR` 应事先指向本次迁移专用目录。若 ETBC 或 IAM 只在特定 Docker 网络可达，
在下列 `docker run` 命令中追加经过确认的 `--network <network-name>`。
镜像支持以任意非 root UID 运行；Linux 交付主机建议追加
`--user "$(id -u):$(id -g)"`，并确保该 UID 可写 `MIGRATION_STATE_DIR`，以免状态文件归属 root。

## 生产运行命令

Preflight（只需 ETBC 秘密，不调用 IAM）：

```bash
docker run --rm \
  --user "$(id -u):$(id -g)" \
  --env ETBC_PASSWORD \
  --mount type=bind,src="$MIGRATION_CONFIG",dst=/run/migration/config.toml,readonly \
  etbc-iam-migration:1.0.0 \
  --config /run/migration/config.toml \
  preflight \
  --legacy-tenant-id "$LEGACY_TENANT_ID" \
  --enabled-modules TENANT,ORGANIZATION,STAFF \
  --source-timezone Asia/Shanghai
```

正式迁移（首次执行会创建固定快照）：

```bash
docker run --rm \
  --user "$(id -u):$(id -g)" \
  --env ETBC_PASSWORD \
  --mount type=bind,src="$MIGRATION_CONFIG",dst=/run/migration/config.toml,readonly \
  --mount type=bind,src="$MIGRATION_STATE_DIR",dst=/state \
  etbc-iam-migration:1.0.0 \
  --config /run/migration/config.toml \
  migrate \
  --batch-id "$MIGRATION_BATCH_ID" \
  --legacy-tenant-id "$LEGACY_TENANT_ID" \
  --enabled-modules TENANT,ORGANIZATION,STAFF \
  --source-timezone Asia/Shanghai \
  --snapshot-at "$SNAPSHOT_AT" \
  --state-dir /state
```

恢复未完成实体（不重新读取 ETBC）：

```bash
docker run --rm \
  --user "$(id -u):$(id -g)" \
  --mount type=bind,src="$MIGRATION_CONFIG",dst=/run/migration/config.toml,readonly \
  --mount type=bind,src="$MIGRATION_STATE_DIR",dst=/state \
  etbc-iam-migration:1.0.0 \
  --config /run/migration/config.toml \
  resume \
  --batch-id "$MIGRATION_BATCH_ID" \
  --state-dir /state
```

生成 JSON 和 Markdown 报告（不需要数据库或 IAM 秘密）：

```bash
docker run --rm \
  --user "$(id -u):$(id -g)" \
  --mount type=bind,src="$MIGRATION_STATE_DIR",dst=/state \
  etbc-iam-migration:1.0.0 \
  report \
  --batch-id "$MIGRATION_BATCH_ID" \
  --state-dir /state \
  --output-dir /state/reports
```

若要让自动化根据最终台账状态做门禁，将 `report` 换为 `verify`；输出内容相同，退出码反映结果。

## 退出码

| 退出码 | 含义 |
|---:|---|
| `0` | 全部实体为 `SUCCESS` 或 `ALREADY_EXISTS`，或 preflight 成功 |
| `2` | preflight、本地校验或 IAM 批次级校验失败 |
| `3` | 存在实体级失败、未完成项或部分成功 |
| `4` | 网络、HTTP 或响应协议错误 |
| `5` | CLI、配置、秘密环境变量或本地状态配置错误 |

只有 `PROCESS_FAILED` 和安全可重放的传输异常会有限次指数退避重试；`VALIDATION_FAILED` 不会自动
重试。HTTP 200 仍会检查外层 `code`、每个实体状态，以及请求和响应结果的一一对应关系。

## Web 迁移控制台

启动 Web 控制台时，`ETBC_PASSWORD` 必须由运行平台注入进程环境；页面没有密码输入框，也不会
把秘密写入 HTML、URL、浏览器存储或子进程参数。`--bind` 接受回环、指定网卡和通配监听地址，
默认仍为 `127.0.0.1`。控制台使用表单令牌与 `HttpOnly; SameSite=Strict` 会话 Cookie 配对进行
CSRF 防护，并提供 CSP、禁止缓存和单操作互斥；正式迁移和 resume 需要在页面上进行明确的 IAM
写入确认。该配对不依赖进程内的单一令牌，因此服务重启或多实例切换不会使已打开页面立即失效。

控制台自身不提供登录认证，IAM 迁移接口也没有 Token 认证。使用 `192.168.x.x`、`0.0.0.0` 或
`::` 等非回环监听时，必须由交付环境的防火墙、网络 ACL 或受控反向代理限制可访问来源；
`0.0.0.0` 表示监听所有 IPv4 网卡，浏览器应使用服务器的实际 IP 访问。

直接使用 Python 3.12 隔离环境时：

```bash
.venv/bin/python -m etbc_migration \
  --config "$MIGRATION_CONFIG" \
  web \
  --state-dir "$MIGRATION_STATE_DIR" \
  --bind 127.0.0.1 \
  --port 8080
```

浏览器访问 `http://127.0.0.1:8080/`。Linux 上使用容器时，可通过 host 网络让容器内的回环监听
保持为宿主机回环监听：

```bash
docker run --rm \
  --user "$(id -u):$(id -g)" \
  --network host \
  --env ETBC_PASSWORD \
  --mount type=bind,src="$MIGRATION_CONFIG",dst=/run/migration/config.toml,readonly \
  --mount type=bind,src="$MIGRATION_STATE_DIR",dst=/state \
  etbc-iam-migration:1.0.0 \
  --config /run/migration/config.toml \
  web \
  --state-dir /state \
  --bind 127.0.0.1 \
  --port 8080
```

在确认 `192.168.137.213` 是服务器本机网卡地址且已配置网络访问控制后，可将上述命令改为：

```bash
  --bind 192.168.137.213 \
  --port 8080
```

同网段交付终端随后访问 `http://192.168.137.213:8080/`。跨不受信任网络时仍应使用具备 TLS、
身份认证和访问日志的组织级反向代理；本工具不自行实现远程认证。

## 测试

Docker 中运行全部单元测试：

```bash
./run-unit-tests.sh
```

运行完整的一次性 Docker Compose 集成测试：

```bash
./run-integration-tests.sh
```

完整集成测试要求本仓库位于 IAM 工作区的 `migration-scripts/`，并与
`iam-management-service/`、`iam-auth-center-service/` 保持同级目录。

集成环境只使用人工构造数据，不暴露任何宿主端口。脚本运行时生成临时数据库密码，
并通过 `trap` 在成功、失败或中断后执行 `docker compose down --volumes --remove-orphans`。测试覆盖
正常迁移、组织层级、故障后 resume、幂等重跑、空邮箱/性别映射、忽略
`sys_user_orgnization`、禁止密码字段传输、默认密码真实登录，以及未请求模块表不发生变化。
同一环境还会在测试容器的回环地址启动 Web 控制台，验证安全响应头、CSRF、秘密/PII 不进入页面、
Web preflight，以及通过 Web 重放真实 IAM 批次后的 `ALREADY_EXISTS` 幂等结果。

## 上线检查清单

- 明确的 legacy tenant ID、全局唯一 batch ID、UTC `snapshotAt` 和正确的 source timezone。
- ETBC 主机、端口、schema、仅 `SELECT` 账号，以及需要时的 CA 文件。
- IAM Management HTTPS 地址、受控网络连通性，以及对无认证内部迁移路由的网络级访问限制。
- Web 非回环监听时的防火墙/ACL、授权访问源和关闭时间。
- 通过秘密管理系统注入的 `ETBC_PASSWORD`。
- 专用的加密状态目录、容量、备份、保留期和销毁责任人。
- 经容量评估的员工分片大小、请求超时、最大尝试次数和退避基数。
- preflight 报告、变更窗口、IAM 侧监控，以及迁移后业务验收人。

演示 ETBC 数据库或本地真实备份只可用于显式启用的只读 preflight/smoke 检查，绝不能作为自动化
fixture，也不得将实际数据行输出到终端或日志。测试不得指向真实 IAM 环境。
