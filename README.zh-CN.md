<p align="center">
  <a href="./README.md">English</a> · <strong>简体中文</strong>
</p>

# CodeMesh V2

> AI 写完代码，CodeMesh 负责把变更交接清楚。

CodeMesh V2 是一个本地优先的软件变更交接与验收工作台。它在 Coding Agent 完成代码变更之后工作，把这次精确变更整理成可复查的记录：改了什么、证据是什么、还剩哪些风险、谁做了决定，以及这个决定对当前代码是否仍然有效。

CodeMesh 不是另一个 Coding Agent。它帮助下一位开发者或 Agent 理解、验证、接受或拒绝一项变更。

> **项目状态：** 本地 MVP 的源码主流程已经合入 `main`。当前用于本地试用和评估，尚未部署，也没有经过生产环境验证。

## CodeMesh 能做什么

- 为待验收的精确代码变更生成稳定的 subject digest。
- 收集 Git 状态、任务与 policy 文档、白名单命令、Artifact 和 Agent Receipt。
- 用确定性的 Policy Gate 执行硬性约束。
- 调用一个显式配置的 Reviewer，目前为 DeepSeek，不自动 fallback。
- 通过本地 FastAPI、SQLite 和 Next.js Workbench 展示验收结果。
- 管理 Change Acceptance Case、时间线、Finding、人工决策、新鲜度检查和 Change Passport。
- 在独立配置和受限 workspace grant 下执行可选修复。

专业 Reviewer Council 仍是实验能力，不在默认评审流程中。

## 默认评审流程

```text
代码变更 + 任务 + Policy
          |
          v
   稳定的 subject digest
          |
          v
   证据收集与 Manifest
          |
          +----> 确定性 Policy Gate
          |
          +----> 固定 Reviewer
          |
          v
 Change Acceptance Case
          |
          v
 人工决策与 Change Passport
```

只要 subject 发生变化，CodeMesh 就会把绑定旧 digest 的证据和决策标记为过期或失效。

## 快速开始：离线 Demo

离线 Demo 是查看 Workbench 最快的方式。它只使用本地确定性数据，不调用模型，也不会写入 GitHub。

### 环境要求

- Python 3.10 或更高版本
- Node.js 20 或更高版本
- pnpm

### 安装

```bash
git clone https://github.com/Brandoo110/CodeMesh-V2.git
cd CodeMesh-V2

make venv
make install
(cd frontend && pnpm install --frozen-lockfile)
```

### 写入 Demo 数据

```bash
./.venv/bin/python -m web.assurance_demo
```

默认 SQLite 文件位于 `~/.codemesh/assurance.sqlite`。Demo seed 可重复执行；如果固定 Case 已存在但内容冲突，它会拒绝覆盖。

### 启动 Workbench

在两个终端分别运行：

```bash
make ui-backend
```

```bash
make ui-frontend
```

浏览器打开 [http://localhost:3010](http://localhost:3010)。本地 API 地址为 `http://127.0.0.1:8010`，健康检查命令如下：

```bash
curl --fail http://127.0.0.1:8010/api/health
```

五分钟操作说明见 [P6_DEMO.md](./P6_DEMO.md)。

## 发起一次真实本地评审

真实评审会调用配置的模型服务，并可能产生费用。只有在待审仓库内容和本次模型调用都已获得授权时才执行。

### 1. 准备 Runtime 配置

```bash
mkdir -p "$HOME/.codemesh/runtime/artifacts"
cp examples/assurance-runtime.v2.example.json \
  "$HOME/.codemesh/runtime/assurance-runtime.v2.json"
```

启动服务前编辑复制后的 JSON：

- 把所有 `ABSOLUTE/PATH` 占位符替换成绝对路径。具体来说，
  `database_path` 应指向 `~/.codemesh/runtime/assurance.sqlite`，
  `artifact_store_root` 应指向 `~/.codemesh/runtime/artifacts`；JSON 中必须写
  展开后的完整绝对路径。上面的命令已经创建了数据库父目录和 Artifact 目录。
- 数据库和 Artifact Store 必须放在待审 workspace 之外。
- `workspace_root` 必须包含目标 Git 仓库。
- 把修复配置中的 `allowed_paths` 换成真实的仓库相对路径。
- 不要把任何密钥写进 JSON。

每次 Run 还需要任务说明和至少一份 Policy。可以从 [examples/quickstart-task.md](./examples/quickstart-task.md) 复制任务模板；任务和 Policy 路径都相对于目标仓库。

### 2. 注入 Reviewer 密钥

在 zsh 中可以用下面的方式读取密钥，输入不会回显，也不会把明文写进 shell history：

```bash
read -s 'CODEMESH_REVIEWER_KEY?DeepSeek reviewer API key: '; echo
export CODEMESH_ASSURANCE_REVIEWER_API_KEY="$CODEMESH_REVIEWER_KEY"
unset CODEMESH_REVIEWER_KEY
export CODEMESH_ASSURANCE_CONFIG="$HOME/.codemesh/runtime/assurance-runtime.v2.json"
```

只有提供独立的修复模型密钥后，Remediation 才会启用。CodeMesh 不会自动选择 fallback provider。

### 3. 启动本地 API

```bash
./.venv/bin/python -m uvicorn web.server:app \
  --host 127.0.0.1 \
  --port 8010 \
  --workers 1
```

### 4. 提交待验收变更

进入目标仓库后执行，并替换示例中的占位内容：

```bash
/absolute/path/to/CodeMesh-V2/.venv/bin/codemesh assurance run \
  --repository "$PWD" \
  --repository-identity "owner/repository" \
  --author "your-name" \
  --base-ref "<existing-base-ref>" \
  --task-path "path/to/TASK.md" \
  --policy-path "path/to/POLICY.md" \
  --command-id "diff-check" \
  --provider-boundary "within_declared_boundary"
```

命令会返回 run ID、case ID、subject digest、Policy Gate、新鲜度、可执行动作和本地 Workbench 地址。目标工作树不变时，重复相同请求会复用确定性的幂等 Key。

服务停止后，清除当前 shell 中的 Runtime 变量：

```bash
unset CODEMESH_ASSURANCE_REVIEWER_API_KEY
unset CODEMESH_ASSURANCE_REMEDIATION_API_KEY
unset CODEMESH_ASSURANCE_CONFIG
```

## 仓库结构

```text
assurance/     变更 subject、证据、Policy、评审与 Passport 合同
web/           FastAPI 组合、路由与本地持久化
frontend/      Next.js Workbench
tests/         Python 合同与集成测试
scripts/       CI 与走查脚本
examples/      Runtime 配置和任务模板
```

## 开发检查

在仓库根目录可以运行：

```bash
make help
git diff --check
(cd frontend && pnpm test)
(cd frontend && pnpm exec tsc --noEmit --allowImportingTsExtensions)
(cd frontend && pnpm lint)
```

## 证据与安全边界

- 所有证据和决策都绑定到精确的 subject digest。
- Provider 密钥只应放在环境变量中，不能进入提交文件或命令参数。
- Provider 异常、缺少证据、无效配置和过期 subject 都会 fail closed。
- 离线 Demo、本地测试、Synthetic CI 和 GitHub Check 都不能证明部署或生产验收完成。
- CodeMesh 不会自动部署代码，也不会自动执行生产操作。

## 文档

- [离线 Demo 走查](./P6_DEMO.md)
- [评测报告与结论边界](./P6_EVALUATION_REPORT.md)
- [Runtime 配置示例](./examples/assurance-runtime.v2.example.json)
- [任务说明模板](./examples/quickstart-task.md)
- [CI 团队接管走查](./docs/p-c-handover-walkthrough.md)

## 许可证

当前仓库尚未添加开源许可证，主要用于私有开发和本地评估。
