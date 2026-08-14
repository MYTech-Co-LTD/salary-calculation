# 架构文件（Architecture Decisions Record）

> 本文件是项目架构决策的**权威记录**，记录每个已确认架构决策的**决策过程**（背景 / 备选 / 理由 / 影响），遵守 `CLAUDE.md` 铁律 3、4。详细设计见 `docs/superpowers/specs/`；本文只锁"是什么 + 为什么"。

- 最近更新：2026-08-14（ADR-024 导入提速：去重直插 + OBS 中转上传）
- 状态图例：✅ 已确认（锁定） · 📋 已规划后续
- **本轮（计算数据流重构）架构已全部确认**（2026-07-19），可进入 writing-plans。

---

## ADR-001 存储引擎：保留 SQLite + 开启 WAL ✅

- **决策**：不迁移 PostgreSQL；SQLite 留用，开启 `journal_mode=WAL` + `busy_timeout`。
- **背景**：用户反馈"导入后计算慢、反复切节点卡死"，担心是 SQLite 性能瓶颈。
- **核实**：经代码核实，慢/卡死的根因（R1 重算、R2 无物化、R3 无并发锁、R4 逐行 INSERT、R5 进面板全扫）**均为应用层问题，无一是 SQLite 天花板**；`compute()` 是纯 Python+Decimal、根本不碰数据库。
- **备选**：迁 Postgres。
- **理由（弃 Postgres）**：场景为单用户、月度批量、零并发、9 万行/月、单 Docker 实例。迁库会把同一套 bug 带过去且新增运维（独立进程/连接池/备份），而 SQLite 的唯一痛点（锁竞争）由 WAL + single-flight 即可消除。
- **影响**：部署仍单文件 `salary.db`；引入 Alembic 做迁移（顺带修 H19 历史缺列）。
- **复评触发条件**：出现水平扩容 / 多租户 / 多副本 / 真高并发 任一需求时，重评是否迁 Postgres。

## ADR-002 计算结果消费模型：算一次 + 全量物化（方案 A）✅

- **决策**：`POST /compute` 跑一次引擎，落库 `Result`（聚合）+ `DetailRow`（逐笔）；所有读端点查物化表，零重算。
- **背景**：现实现把 `compute()` 当"按需重算的瞬态函数"，每次下钻/导出全量重算 → 卡顿；且无 single-flight → 并发叠加卡死。
- **备选**：B 进程内存缓存 ComputeResult（重启即失、staleness 难控）；C 最小止血（只 WAL+single-flight+批量，不物化，下钻仍重算）。
- **理由（选 A）**：唯一能同时满足用户硬需求"精确"（单一真值源，总额=逐笔Σ）与"流畅"（所有点击毫秒级）。B 脆弱、C 不达标。
- **影响**：新增 `DetailRow` 表；compute 写入量 ≈ 导入行数（9 万级/月），SQLite 批量+索引可承受。

## ADR-003 台账与导出粒度：逐行 ✅

- **决策**：台账/导出表按**逐行**——每条导入销售/退货行 = 一行，`commission = 行amount × 组rate`，Σ逐行 = 工资总额（数学精确相等）。
- **备选**：按 (单号,条码) 分组（一组一行）。
- **理由（选逐行）**：最透明、最好对账；行数虽多（9 万级）但带索引毫秒可查。对账闭环：`有效计提`Σ=总额、`赠送`Σ=0、`退货`正负相抵、`调班`可追溯。
- **影响**：导出表 = `DetailRow JOIN SalesRecord`（逐行+全字段）。

## ADR-004 逐行提成的计算来源：引擎直出 DetailRow（M1）✅

- **决策**：改 `compute()` 使其**逐行**产出 DetailRow（组内每条销售/退货行各一行），单一计算源；物化层 1:1 落库，不再推导。
- **备选**：M2 引擎出按组、桥接层展开为逐行。
- **理由（选 M1）**：M2 在桥接层重走分组逻辑，有与引擎漂移的风险——正是本轮要消灭的隐患。M1 保证"计算只有一处"。
- **影响**：`salary_engine/calculator.py` 改逐行发 DetailRow；新增不变量单测 `Σ DetailRow.commission == 总额`。

## ADR-005 计算真值源：SalesRecord（治 C2）✅

- **决策**：`compute()` 改从 `SalesRecord` 读入参（不再读 `_sales_cache` 的 Excel）；Excel 仅作原件存档。
- **背景**：现 `_run_compute` 重读原始 Excel，无视 `SalesRecord`/`TransferRecord` → 拖拽调班、当班修正对计算不可见（C2）。
- **理由**：SalesRecord 是已解析、已打标签、已应用调班转移的真值；以它为源，C2 自然解决。
- **约束**：过滤（赠送/不计提成/非乳品）是**引擎单一职责**；`SalesRecord.tag` 仅供展示，不参与计算过滤，避免双重过滤不一致。

## ADR-006 费率表：合一为 SalaryPolicyVersion（治 C1）✅

- **决策**：`compute` 读 `SalaryPolicyVersion.content`，废弃 `RateVersion`；`Month.policy_version_id` 首次 compute 锁定、重算不覆盖。
- **背景**：UI 写 `SalaryPolicyVersion`、引擎读 `RateVersion`，两套并存 → 策略编辑对计算无效（C1）。
- **理由**：单一真值源；同时修审计 H10（版本锁被覆盖）、rate_version_id 解引用 500、JSON 无结构校验。
- **影响**：一次性迁移脚本 `RateVersion → SalaryPolicyVersion`，迁移后停用旧表。

## ADR-007 修复范围：C1/C2/C3 并入本轮，C4–C6 后续 ✅

- **决策**：C1（费率合一）、C2（真值源）、C3（当班天数）并入本轮重构。
- **理由**：C1/C2 是"真值源"主线直接产物；C3 有明确反例、属规格违背（非引擎口径争议）。
- **C3 风险（用户已确认接受）**：修 bug 后部分人提成数字会变（更准）；交付前在真实 6 月数据做前后对比核验，确认影响面。
- **后续（📋 已规划，不在本轮）**：C4 赠送剔除致匹配退货变未匹配、C5 多人并列当班静默归一人、C6 breakdown 漏 fallback、及 H1/H13/H14 引擎边缘鲁棒性。先用真实 6 月数据验证影响面，再单独 spec。

## ADR-008 台账标签 taxonomy（Claude 定）✅

- **决策**：6 个**互斥**去向标签（按提成命运）+ 1 个**正交**转移标记：
  - 互斥标签：`有效计提 / 退货冲抵 / 退货未匹配 / 赠送剔除 / 不计提成 / 非乳品`
  - 正交标记：`is_transferred`（调班转移），附 `original_store / original_date / transfer_reason`
- **台账逐行全覆盖**：引擎对**每条**导入行产出一条 DetailRow——计提的 `commission = 行amount × 组rate`；剔除的 `commission = 0` 且标签标明原因。单一源、可穷举对账。
- **理由**：
  - 互斥标签覆盖每条导入行的全部命运；对账闭环严格：`Σ(有效计提+退货冲抵+退货未匹配).commission = 工资总额`，其余标签 = 0。
  - **新增"非乳品"**：importer 现把非乳品行误标"有效"但引擎丢弃，台账会显示"有效却 0 提成"的矛盾；显式标"非乳品"消除歧义。
  - **调班转移做正交标记而非标签**：转移行同时也是"有效计提"等，做成标签会丢失提成命运信息；正交标记两者都保留。
- **影响**：引擎需对剔除行也产出 0 提成 DetailRow（commission=0 + 原因标签）；SalesRecord.tag 与引擎命运对齐（补"非乳品"）；导出多一列 `is_transferred`。

## ADR-009 费率存储约定：SalaryPolicyVersion 存百分数，边界 /100 ✅

- **决策**：`SalaryPolicyVersion.content.commission_rates` 存**百分数**（如 `12` = 12%，与 UI 工资策略编辑器一致、人类友好）；`engine_bridge.rates_from_db` 在 DB→引擎边界 **÷100** 转分数（`0.12`）喂引擎。引擎内部仍用分数乘数，不动。
- **背景**：旧 `RateVersion` 存分数（`seed_rate_table` 做 /100）；UI 策略编辑器存百分数。两表约定不同。C1 切换引擎读 `SalaryPolicyVersion` 时，若不做 /100 会产生 **100× 提成误差**（12 当乘数而非 0.12）。
- **备选**：(B) 把策略改存分数与 RateVersion 一致——需动 UI 编辑器+前端+现有数据迁移，范围大，弃。
- **理由（选 A）**：单一转换点（`rates_from_db`），UI 保持人类友好的百分数，引擎口径不变；改动最小、风险最低。
- **影响**：conftest 种子按百分数存；`migrate_rates_to_policy` 脚本对 RateVersion 分数 ×100 后存入；相关测试喂 `"15"` 期望 `Decimal("0.15")`。
- **决策过程**：T3.2 实现中发现（spec 原假设 `Decimal(str(pct))` 直接用是错的，会 100× 误差），按铁律 2 停下报用户；用户选 A（2026-07-19）。

## ADR-010 category=None（成本表有、信息表无）按非乳品排除 + 警告 ✅

- **决策**：条码在销售成本表但不在商品信息表（奶品白名单）→ `category=None` → 引擎按「非乳品」处理（不计提、进台账标"非乳品"）并 emit warning「缺分类：<条码>」，**不崩溃**。
- **背景**：T8.1 真实 6 月数据触发审计 H1——145 个成本表独有条码 `category=None`，3717 行（4.5%）在 `classify_tier(None)` 抛 `ValueError` 中断整次计算。样本混有真奶（信息表漏，如"低温酸奶"）与非奶（烟/水/茶）。
- **备选**：(B) 兜底当特价档计提——会误把非奶计提、多付钱，弃。(C) 先由数据负责人补全信息表——需数据修复，且引擎仍需不崩溃修复。
- **理由（选 A）**：商品信息表是奶品白名单（单一真值源）；不在白名单的不计提，避免多付非奶（更严重的错误）；真奶被排除由 warning 暴露，数据负责人补信息表后重导即正确。引擎健壮性优先（绝不因脏数据崩溃）。
- **影响**：`calculator` 过滤增加 `product.category is None → 非乳品 + 缺分类 warning`；真奶（如低温酸奶）在信息表补全前被排除（少付），靠 warning 提示。
- **决策过程**：T8.1 真实数据验证中发现（审计 H1 在真实数据触发），按铁律 2 停下报用户；用户选 A（2026-07-20）。

## ADR-011 CI 基础镜像走 ghcr.io，移除美国 runner 上的反向国内镜像 ✅

- **决策**：GitHub Actions（美国 runner）构建时，base 镜像（python/node/nginx）从 **`ghcr.io/ylwzzs/`** 拉（由单独的 `mirror-bases` 工作流从 Docker Hub 镜像过去）；**移除**帮倒忙的 `xuanyuan.run` Docker 镜像配置和 backend Dockerfile 的 `pip 阿里云源`（这俩让美国 runner 把流量绕到国内，更慢更不稳）；PyPI/npm 用默认源（美国境内可达且快）。最终镜像仍推天翼云。
- **背景**：CI 跑在美国 runner，base 镜像走第三方 `xuanyuan.run`（不稳）+ Docker Hub 对 GitHub 共享 IP 匿名限流（100/6h）→ 经常拉取失败；pip 阿里云 在美国 runner 上是"反向加速"。推镜像到天翼云是唯一保留的跨境跳（推比拉宽容，可重试）。
- **备选**：(B) Docker Hub 认证——更简单、零镜像维护；(C) 国内构建——要配国内镜像 + 开机器。用户选 ghcr.io。
- **理由（选 ghcr.io）**：GitHub 原生 registry，对美国 runner 最快、**无限流**；`mirror-bases` 一次性播种 + 定期刷新安全更新。代价：需镜像 3 个 base（一份维护脚本）。
- **影响**：新增 `.github/workflows/mirror-bases.yml`（GITHUB_TOKEN packages:write，定期 + 手动）；`ci.yml` 移除 `xuanyuan.run`、加 `packages: read` + ghcr.io 登录；两个 Dockerfile `FROM ghcr.io/ylwzzs/...`、backend 删 `pip 阿里云`。
- **决策过程**：CI/CD 镜像源讨论，用户选 ghcr.io（2026-07-20）。

## ADR-012 base 镜像维护挪到专用仓库 ylwzzs/base-images（public，所有项目共用）✅

- **决策**：base 镜像的 mirror 工作流从 salary-calculation **挪到专用仓库 `ylwzzs/base-images`**（public）；`ghcr.io/ylwzzs/{python,node,nginx}` 设为 **public**，ylwzzs 名下所有项目匿名共用。salary-calculation 删除自己的 `mirror-bases.yml`（只作消费方），`ci.yml` 仍从 `ghcr.io/ylwzzs/*` 拉。
- **理由（取代 ADR-011 里"mirror 放本仓库"的安排）**：解耦——base 镜像基础设施与具体应用仓库分离；任一项目删/归档不影响其他项目；新项目 `FROM ghcr.io/ylwzzs/...` 即可，零配置。public 因为这些只是 Docker Hub 公开镜像的副本，无密钥。
- **维护**：`base-images` 仓库每周自动刷新 + 手动触发；加新 base 镜像编辑其 workflow 的 `for img in ...` 列表。首次使用前去该仓库 Actions 手动跑一次播种。
- **决策过程**：用户在 CI 镜像源讨论后要求“设公共 + 搬专用库”（2026-07-20）。

## ADR-013 构建搬回国内（单独构建机 + 国内镜像仓库），取代美国构建 ✅

- **决策**：废弃 ADR-011 的“美国 GitHub runner 构建”。改为：**国内构建机**（天翼云 ECS 上跑 self-hosted GitHub Actions runner）构建镜像 → push 到**国内镜像仓库**（天翼云 SWR，in-region）→ **部署机只 pull**（in-region）+ up。base 镜像走国内镜像源。
- **背景**：ADR-011 的美国构建把大镜像（backend ~800MB，含 pandas/numpy/pymupdf）**跨境推天翼云**，buildx 在 `#16 exporting to image` 必卡（两次实测都卡死），不可靠；本地在部署机上 build 又有 OOM/抢资源风险影响生产稳定。
- **理由（选国内构建机+仓库）**：国内构建→国内仓库是 **in-region，无跨境**，快且稳（国内生产项目标配）；部署机只 pull 不 build，**生产稳定不受构建影响**；回滚快（pull 旧 tag）。天翼云 SWR 在构建机也在天翼云时是同云 in-region，可用。
- **影响**：需一台国内构建 ECS（小规格，跑 self-hosted runner + Docker）；`ci.yml` 改 `runs-on: self-hosted` + 国内源；Dockerfile 改国内镜像源（阿里云 apt/PyPI、npmmirror npm）；`CI-CD-STANDARD.md` 改为“国内构建”版；天翼云 SWR 保留。
- **决策过程**：美国构建两次卡死在 exporting to image 后，用户选“国内构建机+仓库”（2026-07-20）。

## ADR-015 国内 self-hosted runner CI/CD 落地方案 ✅

- **决策**：国内 self-hosted runner 上，checkout 用 **gitclone.com 镜像手动 git clone**（替代 `actions/checkout`），deploy 用**原生 ssh 命令**（替代 `appleboy/ssh-action`）。
- **背景**：国内构建机（ADR-013）的 self-hosted runner **无法直接访问 github.com**（443 端口被阻），导致 `actions/checkout` 和 `appleboy/ssh-action` 均失败。需一套完全不依赖直连 GitHub 的 CI/CD 方案。
- **踩坑记录**：

  | # | 尝试方案 | 结果 | 根因 |
  |---|---------|------|------|
  | 1 | `actions/checkout@v4` + `git config url.insteadOf` 改写到 xuanyuan.run | ❌ checkout 失败 | `actions/checkout` 是 GitHub Action，有自己的 checkout 逻辑，**不遵守** `git config url.insteadOf` 规则 |
  | 2 | 手动 `git clone` 到工作目录 `.` | ❌ `destination path '.' already exists and is not an empty directory` | self-hosted runner 的工作目录**不会在 job 间清空**（不同于 GitHub-hosted runner），目录非空导致 clone 失败 |
  | 3 | `git clone` 到 `$RUNNER_TEMP/repo` + `working-directory: ${{ env.REPO_DIR }}` | ✅ 成功 | `RUNNER_TEMP` 是干净的临时目录 |
  | 4 | `appleboy/ssh-action@v1` 部署 | ❌ `Failed to download drone-ssh-1.8.2-linux-amd64` | ssh-action 需从 GitHub 下载二进制，国内 runner 无法访问 |
  | 5 | 原生 `ssh -i` 命令部署 | ✅ 成功 | 无外部依赖，self-hosted runner 自带 ssh |

- **镜像选择踩坑**：
  - `xuanyuan.run`（`caj9ik14016wep-ghcr.xuanyuan.run`）只代理 **ghcr.io**（容器镜像仓库），**不代理 github.com**（git 仓库）。`git clone https://caj9ik14016wep-ghcr.xuanyuan.run/...` 会报 `repository not found`。
  - `gitclone.com` 可代理 github.com 的 git 仓库，但有缓存延迟（新推送的 branch 可能需要几分钟才可见），rerun 通常能解决。
  - **gitclone.com 缓存的严重后果（2026-07-22 实测）**：CI 用 `${{ github.sha }}`（GitHub 的值）给镜像打 tag，但 gitclone.com clone 拿到的是缓存的旧 commit 代码 → **镜像 tag 标着新 commit、实际 build 的却是旧代码**，CI 显示 success 但产物错误。本次 entrypoint 部署即因此"成功部署"了无 entrypoint 的旧镜像。
  - **防护**：clone 后 `git rev-parse HEAD` 比对 `github.sha`，不符则 fail（防静默 build 错代码）；滞后时 rerun 等 gitclone.com 刷新。根本解法待定（换 clone 源 / runner 配代理）。

- **最终方案**：
  - **test** job：跑在 `ubuntu-latest`（GitHub 美国 runner），可直接 `actions/checkout`，无网络问题。
  - **build-backend / build-frontend** job：跑在 `self-hosted`（国内 runner）。
    - checkout：`git clone --depth 1` 通过 `gitclone.com` 镜像，克隆到 `$RUNNER_TEMP/repo`。
    - build：`working-directory: ${{ env.REPO_DIR }}` 执行 `docker build`。
    - `docker/login-action` 仍可用（该 action 是 JS 实现，不需要下载二进制，runner 初始化时已从 GitHub 下载）。
  - **deploy** job：跑在 `self-hosted`（国内 runner）。
    - 用原生 `ssh -i ~/.ssh/deploy_key` 执行 `docker compose pull && up -d`。
    - SSH 密钥从 `secrets.DEPLOY_KEY` 写入临时文件，执行后立即 `rm -f` 清理。

- **影响**：`.github/workflows/ci.yml` 完全不依赖 GitHub 直连（除 test job 跑在美国 runner），国内 self-hosted runner 所有步骤可在无 GitHub 访问的环境下完成。
- **决策过程**：三次 CI 失败后逐步定位（2026-07-21），用户确认走 gitclone.com 镜像 + 原生 ssh 方案。

## ADR-016 backend 容器启动前自动跑迁移（治"漏迁移致生产缺列"）✅

- **决策**：backend 镜像加 `deploy/entrypoint.sh`，启动顺序 = `init_db()` 建库 → 跑 `migrations/0*.py`（幂等）→ `exec uvicorn`。每次容器启动自动保证 schema 到位。
- **背景**：2026-07-22 生产"目标创建失败"。根因：`months.results_stale`、`sales_records.extra` 在 ORM 有定义但**从未写迁移脚本**；`create_all` 只建新表、不给老表补列；部署流程从不跑迁移 → 生产库（历史 schema）缺这两列 → 读 Month/SalesRecord 报 `no such column` → GET/POST /months、销售流水导入全部 500。
- **备选**：(B) main.py 应用层启动跑迁移——耦合应用启动、迁移逻辑混入应用；(C) 依赖手动跑——已证明不可靠（本次就漏）。
- **理由（选 entrypoint）**：与应用解耦；启动前保证 schema 最新；迁移幂等可重入（新库跳过、老库补列）；`init_db()` 先建库，解决"库不存在时迁移脚本 `exit(1)`"的首次部署阻断。
- **影响**：
  - 新增 `deploy/entrypoint.sh`（`init_db` + 跑 `migrations/0*.py` + `exec uvicorn`）。
  - `Dockerfile.backend` 加 `COPY migrations/` + `COPY entrypoint.sh`，`CMD` 改为 entrypoint。
  - `migrations/002` 的 `DB_PATH` 改读 `SALARY_DB` env（原写死项目根，容器里会改错库），与 003 统一。
  - 历史已用 003 手动止血生产；本 ADR 让未来部署自动跟上 schema，杜绝同类。
- **决策过程**：目标创建失败排查（实测生产缺 results_stale/extra 两列）后，用户选"Dockerfile entrypoint 跑迁移"（2026-07-22）。

## ADR-017 不计考核店：打标签产出 0 提成 DetailRow（撤销过滤）✅

- **决策**：`exclude_assessment=True` 的店**不过滤**，进计算但归入 excluded，产出 DetailRow（`commission=0`、`tag="不计考核"`），不进 results 汇总（salary/breakdown）。
- **背景**：用户要求台账每条交易记录都打标签。台账 = DetailRow JOIN SalesRecord（ADR-003）。上一轮（6f2fc8e）改为"过滤不计考核店" → 长青店没 DetailRow → 台账缺长青店，与"每行打标签"冲突。
- **备选**：(A) 计算时打标签（选）；(B) 维持过滤（台账缺不计考核店）；(C) 导入打 tag + 台账改数据源（动 ADR-003，改动面大）。
- **理由（选 A）**：符合 ADR-008（每条导入行 → DetailRow + 标签）；"不计考核"是提成命运（commission=0），复用引擎既有 excluded 机制（同赠送/非乳品/不计提成）；excluded 行不进 comm_person/ps_sales/breakdown，results 汇总天然不含。
- **影响**：
  - `calculator.compute` 加 `excluded_stores` 参数，步骤 1 把 excluded 店的行归 excluded（tag="不计考核"）。
  - `_run_compute` 撤销 6f2fc8e 的 sales 过滤，改传 `excluded_stores`；duty_override 排除不计考核店（不算目标/天数，避免"缺目标"误报）。
  - 台账含不计考核店（标不计考核、0 提成），results 不计其提成。
- **决策过程**：长青店排查 + 台账打标签需求分析后，用户选"计算时打标签"（2026-07-22）。

## ADR-018 退货匹配修 C4 + 未匹配 DetailRow 补 sales_record_id ✅

- **决策**：退货匹配加一档——原销售被剔除（赠送/非乳品/不计提成/不计考核）的退货归对应剔除标签（commission=0），不走未匹配负；同时退货未匹配 DetailRow 补 `sales_record_id`。
- **背景**：审查「退货一正一负」逻辑发现两漏洞：C4（ADR-007 列为后续，赠送等退货多扣提成）+ 退货未匹配 DetailRow 漏 `sales_record_id`（台账 JOIN 缺字段，生产 24 行全 None）。
- **理由**：剔除销售的退货应同命运（0 提成），不该走未匹配算负；sales_record_id 是台账 JOIN SalesRecord 的键，漏传导致台账不完整。
- **影响**：calculator 步骤 1 后建 `excluded_index`，步骤 2 退货匹配加 elif（原销售在剔除索引→归剔除）；`:193` 补 `sales_record_id`。不修设计口径（未匹配时点、退货 tier 按退货单价，C2）。
- **决策过程**：退货一正一负审查后，用户选"漏洞1+C4"（2026-07-22）。spec：`docs/superpowers/specs/2026-07-22-return-matching-c4.md`

## ADR-019 退货简化：统一按负数算，不关联原单号（谁退扣谁）✅

- **决策**：退货统一按 amount 负数算（commission=amount×rate，**退货当天当班人**+该人×店档），不再用 src_order 关联原销售做精确匹配；台账标签统一「退货」（删退货冲抵/退货未匹配）。
- **背景**：原精确匹配退货为 3 目的（达成率净额、提成归原销售当班人、防同单重复冲减）。行政保证「自己出自己退、全单退、金额对称」+ 用户确认「**谁退扣谁**」（退货提成归退货当天当班人），使精确匹配唯一不可替代的作用（归原销售当班人）失效。
- **理由**：月聚合同人同店下，简化与精确匹配等价（达成率净额一致、提成同人）；消除 src_order 数据依赖（24 行退货未匹配问题消失）；删 C4/精确/未匹配区分，计算大幅简化。
- **影响**：calculator 删 C4 `excluded_index`、精确匹配（groups returns）、精确退货循环；`group_net` 只 sales；`daily_sales` 含所有 returns；所有 returns 走统一负数逻辑（tag=退货、退货天当班人、sales_record_id）。台账标签「退货」统一。**撤销 ADR-018 的 C4 部分**（简化后无精确/未匹配/C4 区分；漏洞1 在统一逻辑里天然带）。
- **决策过程**：退货设计讨论，用户确认「谁退扣谁」（2026-07-22）。spec：`docs/superpowers/specs/2026-07-22-return-simplify.md`

## ADR-020 Web 导出三 sheet（计算结果 + 台账 + 考勤排版）✅

- **决策**：`/months/{month}/export` 改为导出**三 sheet**：Sheet1「计算结果」= 按人×店的聚合汇总（员工/门店/门店类型/月目标/日目标/考勤天数/实际目标/销售额/达标率/达标档/提成金额 + 5 档明细：销售/比例/提成）；Sheet2「提成台账」= 现有逐笔明细（24 列，保留）；Sheet3「考勤排版」= 门店×日期的当班矩阵。全部读物化表，**零重算**（守 ADR-002）。
- **背景**：用户反馈"导出计算结果跟实际脱节"——Web 导出只有逐笔台账 sheet，**完全没有**页面「计算结果」列表（按人×店的汇总：目标/考勤/销售额/达标率/提成），导出物与所见不一致。CLI `exporter.py` 反而有计算结果 sheet（Sheet1），Web 缺，两边不一致。
- **备选**：(A) 双 sheet（计算结果 + 台账）→ 用户再补考勤排版 = 最终**三 sheet**；(B) 只导出汇总表，删逐笔台账——丢逐笔审计追溯；(C) 逐笔台账里塞汇总列——混在一起，不推荐。
- **理由（选三 sheet）**：Sheet1 对齐页面列且含 CLI 已有的 5 档明细（满足"计算结果完整数据"）；Sheet2 保留逐笔流水，保 ADR-003 对账闭环（逐笔 Σ = 汇总 Σ）；Sheet3 满足"把考勤排版也导出来"（门店×日期矩阵，与前端 DutyGrid 一致）。
- **影响**：`ledger_export.py` 单 sheet writer 扩为三 sheet；`/months/{month}/export` 增查 `Result` 聚合 + `DetailRow` 5 档分组（可从已查逐笔行聚合，不加查询）+ `MonthlyTarget`/`Duty`/`Store` 派生列 + `Duty` 矩阵。Sheet1 数据源 = `Result` 表（与页面同源）。**边界**：无当班人的销售只进 `DetailRow` 不进 `Result`（compute 既有语义，ADR-008），此时 Sheet2 Σ 略大于 Sheet1 Σ；6 月实测无此类行，Σ 一致（85560.92）。
- **决策过程**：用户确认方案 A 并追加考勤排版 sheet（2026-08-06）。spec：`docs/superpowers/specs/2026-08-06-export-three-sheets.md`

## ADR-021 导出预生成缓存 + xlsxwriter 写器 ✅

- **决策**：`/months/{month}/export` 改为**缓存优先**：compute 成功后**后台线程预生成**导出文件到 `/data/export_cache/salary_{month}.xlsx`；导出端点缓存命中且非 stale 时**直接读文件秒开**，未命中/失效时兜底同步生成。写器从 openpyxl 换 **xlsxwriter**（C 写器）。
- **背景**：导出 95k 行台账端到端 26s，拆解：SQL 0.8s + dict 0.4s + **openpyxl 写 2.28M 单元格 ~24s**（瓶颈，纯 Python 序列化）。导出内容完全由计算产物决定（Result/DetailRow/Duty/MonthlyTarget），结果算完即可预生成。
- **备选**：(A) 仅换 xlsxwriter——26s→~10s，但每次导出仍等；(B) 缓存+懒生成（首次导出才生成）——首次仍 10s；(C) compute 后**后台预生成** + 懒兜底（选）。
- **理由（选 C）**：结果算完 ~10s 后台生成完毕，之后导出秒开（用户反复导出同月结果，命中率高）；懒兜底覆盖缓存未就绪/失效/冷启动；xlsxwriter 让预生成从 26s 压到 ~10s，缓存更快就绪、兜底也不慢。
- **影响**：
  - `ledger_export.py`：`write_salary_export` 用 xlsxwriter 重写（3 sheet，原子写 tmp+rename）；`build_summary_rows`/`build_duty_grid` 不变（对账/列序不变）。
  - `workflow.py`：抽 `_build_export_file(db, month, path)`；`/export` 读缓存（`results_stale or status != computed` 判失效）+ 兜底生成 + 原子替换进缓存；`/compute` 成功后 `threading.Thread(daemon)` 预生成（**独立 `SessionLocal()`**，请求会话响应后即关）。
  - `requirements.txt` 加 `xlsxwriter`；镜像重建。
  - 缓存目录：`/data/export_cache/`（持久卷，容器重启保留）；文件名 `salary_{month}.xlsx`。
  - 失效：沿用 `results_stale`（ADR-014 全覆盖）；重算自动重新生成。
- **决策过程**：用户提出"算完即可跑缓存"（2026-08-06），确认后台预生成 + xlsxwriter。spec：`docs/superpowers/specs/2026-08-06-export-cache.md`

## ADR-022 导出经天翼云对象存储（OBS）下载 ✅

- **决策**：`/months/{month}/export` 在上传导出文件到天翼云 OBS（桶 `mytech-lesson`）后，返回 **302 重定向**到公网**签名 URL**（15 分钟有效），浏览器从 OBS 高带宽下载。**未配 OBS 时回退直接下载**（兼容）。
- **背景**：导出生成已优化到 0.03s（ADR-021 缓存），但下载 7.2MB 受**服务器上行带宽**限制（两台不同机器实测 ~150-350KB/s ≈ 2-3Mbps，云主机带宽封顶），20-45s。
- **备选**：(A) 302 重定向（前端零改动，桶需 CORS）；(B) 前端拿 URL 新开窗口（免 CORS，改前端 + 重建）。
- **理由（选 A）**：前端零改动、只重建后端；上传走**云内网**（`xinan-1-internal`，实测 0.01s 连通）快且省公网流量；签名 URL 短期有效不裸奔；桶 CORS 用 AK/SK 通过 S3 API 一次性配置。
- **影响**：
  - 上传：内网 endpoint `http://xinan-1-internal.zos.ctyun.cn`（path-style），区域 `xinan1`，key `salary/{month}.xlsx`。
  - 下载签名：公网 virtual-host `https://mytech-lesson.xinan1.zos.ctyun.cn`（`xinan1.zos.ctyun.cn` + virtual 寻址），region `xinan1`。
  - 新增 `backend/app/services/oss_export.py`：`upload` / `presign` / `head`（本地 mtime vs OSS last_modified，新则覆盖）。
  - `/export`：OSS 已配 → 确保上传 → 302 签名 URL；未配 → 直连下载。compute 预生成后也上传。
  - env：`OSS_ENDPOINT_INTERNAL` / `OSS_ENDPOINT_PUBLIC` / `OSS_BUCKET` / `OSS_ACCESS_KEY` / `OSS_SECRET_KEY` / `OSS_REGION` / `OSS_PREFIX`（默认 `salary/`）。
  - `requirements.txt` 加 `boto3`。
- **决策过程**：用户确认走天翼云 OBS（2026-08-06），提供 AK/SK/桶 `mytech-lesson`/内网+公网 endpoint。spec：`docs/superpowers/specs/2026-08-06-export-oss.md`

## ADR-023 赠送件数不符：异常排查 + 人工确认按件数扣除 ✅

- **决策**：销售流水与让利明细按 `(receipt,barcode)` 匹配后，若**销售件数 ≠ 让利表赠送件数**，进异常排查（type "7"）；用户在 AnomalyPanel 点"确认扣除"后，compute 按 `min(赠送,销售)` 件、**按件数均摊金额**扣除，剩余计提成，该组 DetailRow 打"赠送扣除"标签。**件数相等组维持现状**（自动整组剔除，零变化）。
- **背景**：原 `(receipt,barcode) in gift_keys` 命中即整组剔除（`calculator.py:67`），**无数量意识**。6 月真实数据交叉验证：gift_keys 2444 个 100% 命中，销售件数全部 == 赠送件数（"买多于送" 0 个）→ 现状精确；但理论存在"同组买3送1"会被整组剔除（多剔2件）的静默风险。
- **备选**：(A) 维持现状 + 仅文档记录假设（YAGNI，0 案例，但未来异常静默多剔）；(B) 进异常排查 + 确认扣除走"独立表 + compute 参数"正路（选）；(C) 自动精确按数量剔除（无人工把关，金额拆分口径未确认即生效，风险高，弃）。
- **理由（选 B）**：复用现有异常排查体系（AnomalyChecker / Anomaly 表 / AnomalyPanel 前端软门禁）+ 复用 duty_override 的"确认影响计算"正模板（独立 GiftDeduction 表 + compute 新参数）。**关键：确认状态存独立 GiftDeduction 表而非 Anomaly 表**——Anomaly 表是快照式（每次 check 先 delete 再重建，`workflow.py:253`），存那里会被下次检查清掉。calculator 只需新增 `gift_deduction` 参数，**`gift_keys` 保持 set 不改、importer 不改**（checker 内部独立解析 xlsx 拿数量），影响面最小。
- **口径（用户确认 2026-08-10）**：
  - 扣除件数 = `min(赠送件数, 销售件数)`，**一键确认**（不弹输入框）；销售 < 赠送 → 扣到销售件数为止（全剔）+ 标异常。
  - 扣除金额 = 组总额 × (扣除件数 / 销售件数)（**按件数均摊**，与单行单价差异无关）；件数相等时与"整组剔除"金额完全一致。
  - 不符但被"忽略" → 维持整组剔除（现状），异常说明告知。
  - 原因 / 处理情况存 `GiftDeduction.reason/resolution`；**不改原始 SalesRecord**。
- **影响**：
  - 新表 `GiftDeduction(month,receipt,barcode,sales_qty,gift_qty,deduct_qty,reason,resolution,status,created_at)`，唯一键 (month,receipt,barcode)；迁移脚本（ADR-016 entrypoint 自动跑）。
  - `anomaly_checker.py` 加 `check_gift_qty_mismatch()`（解析 gifts xlsx"数量"列 vs SalesRecord 聚合件数，产 type "7"；已确认组不重复产；**退货赠送 gift_qty≤0 豁免**——走现有 gift_keys 剔除，不进 type7）。
  - `calculator.py` 加参数 `gift_deduction={(receipt,barcode):deduct_qty}`；命中组分叉——在 gift_deduction 则进聚合按均摊扣（tag "赠送扣除"）、否则整组剔（现状）。
  - `engine_bridge.py` 加 `gift_deduction_from_db`；`workflow.py:_run_compute` 装配 + 新端点 `POST /months/{month}/gift-deduction/confirm`（仿 PUT /duty）。
  - 前端 `AnomalyPanel` ANOMALY_TYPES 加 "7" + "确认扣除"按钮；`api.ts` 加封装。
  - `db.py` 注释 1-6 → 1-7；`test_workflow.py:392` known_tags 加"赠送扣除"。
  - 不让利明细整体落库；不弹窗输入件数；不给 SalesRecord 加列；不改 importer。
- **决策过程**：用户问"同小票买多件只送1件怎么处理"（2026-08-10）→ 6 月数据验证 0 案例 → 用户先选"加异常不改计算"，再明确为"进异常排查 + 确认扣除 + 打标签 + 注明原因和处理情况" → 金额口径选"按件数均摊"、件数口径选"默认扣 min(赠送,销售) 一键确认"。**实现后 T9 真实 6 月数据验证发现退货赠送（gift_qty 负数）被误报 type7（4 个），用户裁定豁免 gift_qty≤0（2026-08-10）**。spec：`docs/superpowers/specs/2026-08-10-gift-qty-mismatch-design.md`

## ADR-024 导入提速：内存去重直插 + OBS 中转上传 ✅

- **决策**：销售流水导入（7 月 28MB、10.2 万行，全程 60-90s）两项优化：
  1. **落库提速（纯性能优化，不改数据流）**：`import_sales_to_db` 弃 `on_conflict_do_update` upsert，改为**内存 dict 去重（同文件重复行后写覆盖前写）+ Core `insert` 批插**。唯一键与现有 on_conflict `index_elements` 完全一致（month/receipt/store/sale_date/barcode/amount），语义零变化。实测 42s → 8.2s。
  2. **上传走 OBS 中转（数据流变化）**：前端先向后端要**预签名 PUT URL**（需登录）→ 浏览器**直传 OBS 公网 endpoint** → 前端通知后端 → 后端从**内网 endpoint** 拉回文件落 uploads 目录 → 复用现有解析/落库逻辑。**未配 OBS 时前端回退 multipart 直传**（旧端点保留，兼容本地开发）。
- **背景**：实测分解（2026-08-14，生产容器 + 临时库基准）：网络传输 15-30s / Excel 解析 5.8s / **SQLite 落库 42-47s（大头）**。落库慢根源是 `on_conflict_do_update` 逐行索引探测（WAL 条件下 47s，甚至慢于默认 journal 的 36s；`synchronous=NORMAL` 仅省 ~5s → 证明瓶颈不在 journal 模式）。另：上传流量不再过 openship-edge，**天然绕开边缘 body 上限**（同日刚踩 1MB 上限 413 坑，已调 100m）。
- **备选**：
  - 落库：(A) PRAGMA 调优（实测收益 ~5s，弃为主线）；(B) 内存去重 + 纯 executemany insert（**选**，5 倍提速）；(C) 维持现状。
  - 上传：(A) OBS 预签名直传（**选**，复用 ADR-022 基建）；(B) 异步导入（体验"秒回"但实际总时长不变，引入状态机复杂度，弃）；(C) 维持 multipart 直传（受用户→源站公网带宽限制）。
- **理由**：落库 (B) 与 upsert 语义完全一致（dict 覆盖 = on_conflict set_ 后写胜）且 5 倍收益；上传 (A) 与 ADR-022 导出下载**对称**（导出=后端内网上传/浏览器公网签名下载；导入=浏览器公网签名上传/后端内网拉取），复用 `oss_export._clients()` 双 endpoint 模式，28MB 流量不占源站带宽。
- **影响**：
  - `sales_importer.py`：去重直插重写 `_bulk_upsert` 语义（同文件重复行回归测试锁定）。
  - 新 `backend/app/services/oss_upload.py`：`presign_put(key, expires)`（公网 virtual-host 签名 PUT）+ `fetch_to_file(key, path)`（内网拉取 + 拉完删对象，防桶膨胀）。
  - 新端点：`POST /months/{m}/upload-ticket {kind}` → `{url, key}`（key=`salary/uploads/{month}/{kind}-{uuid}.xlsx`，10 分钟有效）；`POST /months/{m}/import-oss {kind, key}`（**校验 key 前缀格式**，防登录用户拉桶内任意对象 → 下载 → 复用现有导入）。
  - 前端 `ImportStep`：ticket → XHR PUT（带进度条）→ import-oss；任一步失败回退旧 multipart 直传；`api.ts` 加封装。
  - **桶 CORS 需加 PUT**（现仅 GET，ADR-022 时配置）：部署时 boto3 `put_bucket_cors` 一次性配置。
  - 预期总时长：60-90s → **~15-20s**（传输数秒 + 解析 5.8s + 落库 8s）。
- **决策过程**：用户反馈"上传太久"（2026-08-14）→ 容器内临时库分阶段基准定位落库为大头、传输次之 → 给方案 A（落库提速）/B（异步）/C（OBS 直传）→ **用户选 A+C 组合**。spec：`docs/superpowers/specs/2026-08-14-import-perf-design.md`

## ADR-014 主数据变更标 stale（治 H1）✅

- **决策**：主数据端点（stores/products/import_master 增删改）成功后，对所有 Month 置 `results_stale=true`。
- **背景**：维度1审查发现这些端点全不标 stale → 改门店类别 / 商品 category / cost / exclude_commission 后，已计算月份仍 `stale=False`，`/results` 喂陈旧物化结果（违背 ADR-002"物化表与输入同步"前提）。
- **备选**：(A) 标所有月份（推荐）；(B) 只标当月；(C) 不标。
- **理由（选 A）**：主数据跨月共用（store_class 决定费率档、category 决定非乳品过滤、cost 决定毛利档），变更影响所有已计算月份；标 stale 仅提示（不强制重算），用户决定是否重算历史。draft 月份本就 stale（status≠computed），标 `results_stale` 无副作用。
- **影响**：新增 `mark_all_months_stale(db)` helper（单 SQL 批量 update）；stores(4)/products(3)/import_master(2) 共 9 个端点 commit 前调用；TDD 测试覆盖"改主数据→computed 月份 stale"。
- **状态**：✅ 已确认（2026-07-21，用户审批；标所有月份 + 9 端点）。spec：`docs/superpowers/specs/2026-07-21-master-data-staleness.md`

---

## ✅ 本轮确认项（2026-07-19，用户已全部同意）

1. C3 修复后部分人提成数字会变（更准）——**接受**，交付前真实 6 月数据前后对比核验。
2. 现有 `salary.db`（38MB）上 Alembic 平滑升级——**同意**。
3. 标签 taxonomy——**由 Claude 定**（见 ADR-008）。
4. 整体设计——**无遗漏**。

---

## 相关文件
- 详细设计 spec：`docs/superpowers/specs/2026-07-19-compute-dataflow-redesign-design.md`
- 审计依据：记忆 `audit-2026-07-19-findings`、`plan1-engine-validated`
- 协作规则：`CLAUDE.md`（铁律）
- 关键代码：`salary_engine/calculator.py`、`backend/app/routers/workflow.py`、`backend/app/services/{engine_bridge,sales_importer}.py`、`backend/app/db.py`
