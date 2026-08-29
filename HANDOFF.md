# Wish Builder Project Handoff

交接時間：2026-08-29（Asia/Macau）

## 一句話現況

Wish Builder 的本機 M1 功能、M1-13 CI gate 與 release verifier 已落地：Trellis 建立和維護可編輯任務圖；Wish Builder 匯入、驗證並鎖定人已批准的 material graph，再負責准入、派工監督、結果驗證、Journal 和崩潰恢復。唯一支援的 Trellis 基線是官方 `0.6.15`；`0.7.0-dev.2` 是已撤回的本機測試 fixture，從未是官方 Trellis release。完整非效能 suite、coverage、mutation 與 changed-safety 證據已綁定候選 commit 通過，本分支也已推送到 `origin/release/m1`；GitHub repository 與 distribution matrices 仍待在同一候選 revision 上跑完。現有持久 backend 證據只有 Windows Pi 的啟動／handshake，沒有送出 model turn；Windows Oh My Pi 仍受阻於未設定的 model/provider credential，本輪沒有要求或使用 credential；Codex 與其他 cell 仍是 fixture 或待跑 CI。正式派工仍未開放。Release publication 另受 GitHub 治理阻擋：repository 尚未建立 `release` environment，而目前 private repository 方案無法啟用所要求的 protected-branch 規則。

## Repository 狀態

| 項目 | 值 |
| --- | --- |
| Repository | `C:\Users\chonk\Documents\Codex\2026-08-15\new-chat\outputs` |
| Branch | `release/m1` |
| M1 candidate 的 base HEAD | `698f8710aef9601eb29445f18c085e6427c36c7a` (`feat: add strict M1 contracts and validation`) |
| M1 candidate revision | 本文件所在 commit；接手時執行 `git rev-parse HEAD` 取得，不在 commit 內自我引用 SHA |
| 工作樹 | tracked files 已提交；接手時仍應以 `git status --short` 核對本機 evidence files |
| Remote | `origin = https://github.com/chriskcl/wish-builder.git`；`release/m1` 已推送，repository 仍是 private，尚未建立 Pull Request 或 release |
| Python package | `wish-builder 0.1.0.dev0`, Python `>=3.11` |
| License | `GPL-3.0-only` |
| Skill ZIP | `wish-builder-skill.zip` |
| Skill ZIP SHA-256 | `974cd75b5fb2e5c454f1d642e7d40c6288718728b695cb28a5897586dbcf8b48` |
| Skill ZIP 大小 | `525,690` bytes |

`changed-lines.json`、`mutation-report.json` 和 `safety-evidence.json` 是本機 evidence，不屬於提交內容；不要把它們加入 commit。所有 tracked 成果已提交並推送。

## 責任邊界

```text
使用者提出方向
        |
        v
office-hours + gstack 整理產品、設計和工程方案
        |
        v
Gate A：人批准產品範圍與架構
        |
        v
Trellis 建立候選任務、依賴與任務上下文
        |
        v
Wish Builder 匯入、驗證並產生 execution-manifest
        |
        v
Gate B：人批准由 task records 投影出的 material graph 與 Wish Builder 衍生 digests
        |
        v
鎖定後執行、驗證、恢復與合併准入
```

- Trellis 擁有可編輯任務圖、任務拆分、依賴、上下文、生命週期和歸檔。
- Wish Builder 不建立第二套拆分引擎或任務資料庫。它擁有 Gate B 後不可變的 execution snapshot、admission、fencing、Journal、recovery 和 merge admission。Gate B 鎖定的是由穩定 task-record 讀取投影出的 material graph；status、progress 和其他 lifecycle-only 變動在 graph digest 不變時不會單獨使 Gate B 失效。
- 每個 gstack review 在獨立、非互動的子工作階段執行。子工作階段只暫時採用 review 明確標示的 recommended 選項，並交回結果、替代方案、可回復性與技術理由；不得把 gstack 建議當成人已批准。
- 只有容易撤回的純工程選擇可自動記錄。產品、架構、成本、安全與其他重要決定必須改寫成白話後集中到 Gate A；子工作階段若回傳 raw question 或不完整 decision data，流程 fail closed。
- active M1 只實作 `wish_builder` scheduler：Wish Builder 依凍結圖排程，Trellis 保存任務和進度。未來才可能加入 Trellis-owned scheduler，且不能和 Wish Builder 同時派工。
- 未來 Trellis scheduler 模式中的 `GraphIndex` 只作安全驗證與恢復索引，不是第二個 dispatcher；active manifest v2 尚不能表示該模式。

## 已實作

- Strict JSON contracts、immutable models、hostile input limits 和 deterministic diagnostics。
- Trellis graph import，以及 `Trellis graph -> execution-manifest.json` 的確定性轉換。
- 官方 `@mindfoldhq/trellis@0.6.15` 與 `@mindfoldhq/trellis-core@0.6.15` 的精確版本、npm integrity、archive hash 與 package tree pin；禁止 `@latest` 或 prerelease 作為安裝基線。
- Graph snapshot 和 revision digest 明確是 Wish Builder 的衍生格式，不宣稱為官方 Trellis API。
- 單一 projection writer 直接寫回權威 Trellis repository；每次操作重驗 workspace、branch 和 task-store 邊界，執行寫前／寫後 digest 檢查。寫前 digest 衝突立即 fail closed，不重新讀取新內容後自動覆寫。
- Manifest v2 的 `trellis_revision` 是 Wish Builder 衍生的 provenance digest，並包含 graph digest、task ID mapping 和 Gate B 失效規則；它不是官方 Trellis revision token。
- Durable append-only Journal、Gate decisions、lease／epoch fencing、checkpoint、streaming replay 和 GraphIndex 重建。
- 相同外部 operation 的恢復互斥鍵綁定 `Journal recovery scope + run_id + operation_id`，可跨獨立 component 防止重試兩次，同時不會把不同 Journal 或無關 operation 串行化。
- Foreground coordinator、worker result admission、`wishctl decide`、ready-set 和資源限制。
- Narrow fake ports 與 durable effect receipts；已發生但尚未記錄的 effect 會 reconcile，不會盲目重派。
- Windows／POSIX process containment、timeout、輸出限制和失敗時 fail closed。
- Windows lease-owner probe 會同時檢查建立時間與 process exit time；已退出但仍被 handle 暫時保留的 process object 不再誤報為 `EXACT_ALIVE`。
- Temporary Git attempt worktree、owned-path 驗證、staging、canonical promotion order、cleanup 和 quarantine。
- Deterministic trace／export，以及 package source 到 standalone Skill runtime 的同步與 parity 檢查。
- 真實 subprocess E2E：Wave 0、兩個時間區間重疊的 Wave 1、Wave 2；完成順序可相反，但 promotion 仍按 canonical order。
- 一般專案 `unittest` 驗收指令已透過 `ProcessAcceptancePort` 在 materialized promotion candidate 內跑完整 lifecycle；只有候選內容通過後 target branch 才會前進。
- 13 個 production crash boundary 的直接證據：5 個 fake-effect subprocess E2E，以及 8 個 Git create／stage／promotion／remove integration；全部直接命中 failpoint，restart 後不重複 mutation，也不誤改 target、ref 或 sibling worktree。
- M1-13 三層 coverage floor、16 項 targeted safety mutation、coverage＋mutation 聯合證據、Windows／Linux performance evidence，以及一次建置、六格共用同一 artifact 的 distribution clean-install CI jobs。
- Release verifier 只從 trusted workflow SHA 重建 wheel、sdist、兩份 Skill ZIP 與 distribution evidence，再逐 byte 比對 CI artifacts；build toolchain 由完整 wheel hashes 鎖定並使用 `--no-isolation`。Release environment 必須有 required reviewer、禁止 self-review，且只允許 protected branches。

## 尚未完成或未開放

- 六個 Pi、Oh My Pi、Codex 平台 cell 都仍是 `artifact: null`、`enabledForDispatch: false`。Windows Pi 只有啟動／handshake 證據，沒有 model turn；Windows Oh My Pi 的 live turn 需要已設定的 model 和 provider credential，本輪沒有要求或使用 credential，因此只能記為 `blocked_credentials`；本機 probe 不能代替完整資格。開放前必須補齊真實 model turn、active cancellation、當機重啟後不重送的 reconcile、cleanup、平行重疊和平台證據，且證據要有獨立可信的 CI/provider attestation 或可核對的原始 event log。Content digest 只證明內容未變，不能單獨證明真的跑過。
- 官方 Trellis `0.6.15` 沒有可靠的跨程序 CAS，因此 projection 維持單一 writer，寫前後核對 digest，衝突或結果不明時 fail closed。backend worker 只寫隔離 Git worktree 和 Journal，不寫 Trellis；Agent 派工和 Trellis projection 是兩條獨立准入線。backend cell 只因自身 live 資格未完成而維持 `enabledForDispatch=false`，不再等待 projection CAS。
- Claude Code 和 macOS 明確延後，不屬於 v1。
- 不含 AI PRD-to-task decomposer、任務 CRUD、另一套 task DB／看板或第二個 scheduler。
- M1 只保留 Python 控制層與 Trellis／backend 整合；其他工具鏈不在目前範圍。
- 不含真實 GitHub adapter、provider 憑證、sandbox、background supervisor／broker、cockpit 和正式部署。
- M1-13 的 CI 規則與本機 gate 已完成；候選 commit 的完整非效能 suite 為 `1479` tests 全綠（`3` skipped），changed-safety evidence 也已綁定候選 HEAD 通過。關閉 M1-13 前仍需由同一個最終候選 SHA 通過第一次 GitHub repository 與 distribution Windows／Linux、Python 3.11／3.12／3.13 matrices。
- GitHub `release` environment 目前不存在；release verifier 會 fail closed。建立 environment 後仍需至少一位 required reviewer、`prevent_self_review=true` 與 protected-branch deployment policy。當前 private repository 方案無法提供所需 protected branches，因此 publication 需先決定升級方案或調整 repository visibility，不能繞過 gate。
- 已採 GPL-3.0-only 並加入第三方 notices；remote 已設定，但 repository 仍是 private，也沒有公開 release。

## 驗證基線

以下首段是已提交候選版本的本機證據。三個 evidence JSON 保留在本機，不加入 repository；遠端 CI 證據仍待產生。

```text
2026-08-29 committed-candidate refresh (Python 3.12.13):
  Non-performance suite under Coverage.py: 1,479 run; OK; 3 skipped
  Performance suite: 16 passed
  Coverage gate:
    contracts/kernel: 95.395242% (floor 95%)
    services: 91.879252% (floor 90%)
    adapters/processes/CLI: 90.663616% (floor 85%)
  All 17 designated safety source files have direct evidence
  Candidate-bound changed-safety evidence: passed; 2,613 changed branches; 16 invariants
  Safety mutations: 16/16 killed (100%)
  Mutation report SHA-256: d2c6830a83d9f847fde9a29af6d260107042a6efcec4df835ff1420f00c0b826
  compileall: passed; git diff --check: passed
  Standalone Skill source/runtime graph parity: passed
  Runtime manifest graph hash: matched
  Deterministic Skill ZIP rebuild and clean extraction: passed
  Skill ZIP SHA-256: 974cd75b5fb2e5c454f1d642e7d40c6288718728b695cb28a5897586dbcf8b48

2026-08-25 historical local baseline (pre-current changes):
  Python 3.11: 1,447 run; 1,444 passed; 3 skipped; summary JSON SHA-256 05b631efabf942cefc2188366b89de895a5b7db127712131f02ec0b169d4952b
  Python 3.12: 1,447 run; 1,444 passed; 3 skipped; summary JSON SHA-256 e81c5d523f634619b44eea11c3acca8ee97caf49ba1676c9fafc80183429d7e2
  Python 3.13: 1,447 run; 1,444 passed; 3 skipped
  Packaging tests: 220 passed
  Standalone Skill runtime tests: 13 passed; Skill validator: passed
  Performance tests: 16 passed
  Crash-boundary registry: 13/13 proven (5 subprocess E2E, 8 integration)

  Coverage gate:
  contracts/kernel 95.395242%
  services 91.638225%
  adapters/processes/CLI 88.033012%
  coverage.json SHA-256: 3e7f7fc8b379e9a9c8fdd889e39c298ecaf1bdb7ac92d2403ce191d6b6554dcb
  coverage-gate.json SHA-256: 0d5b29829561e062598cab9963d659eb3965612031eabbb2f835493d7b6c576a
Safety mutations: 16/16 killed (100%)
Mutation report SHA-256: d2c6830a83d9f847fde9a29af6d260107042a6efcec4df835ff1420f00c0b826

Controlled performance:
  cold replay p50/p95: 10,500/10,521 ms
  checkpoint tail p50/p95: 4/6 ms
  graph batch p50/p99: 1,107/1,118 ms
  peak RSS: 49,668,096 bytes
Performance policy digest: sha256:82ebc88c953002a5247c3a40a7ddb714c38c612150d99f2239c8d78cc70d57e2
Performance evidence JSON SHA-256: bae68300c457336eebe19c44bad60887c65a4a226f00cba84b91a0381e1e1748
Performance baseline: absolute gate passed; relative baseline not checked

Historical local artifacts (work/final-20260825-r2/dist; pre-current changes):
  Wheel SHA-256: 9820c7f2270f00a573a2c87dd8590449c11aeff2fa17cc915cde1afa192228bb; 2,252,626 bytes
  Sdist SHA-256: ade77c38b42b3febda3fdf6d39d640f7b9661c9bfb06e3947e236e83833e37e3; 2,498,858 bytes
  Skill ZIP SHA-256: 15cd6454c8983a1a678287a7a10795b860c5918ef97dfc4e938182cbeae31139; 524,554 bytes; repeat byte-identical
  Distribution evidence digest: sha256:1472ba49ddcb5641b2bb7893d99eb750c67d014106037cc577bfca9af6d89296
  Distribution evidence JSON SHA-256: 28459e77508addcd5529988269ce3d26f9842074c316edd232f4c7427a4795a0

Windows clean-install of the same wheel and sdist:
  Python 3.11: passed; evidence sha256:dc9aec863074c8d22c83dfc9344801df2b6ede1886b2c309e128cb6f07963ba6; JSON SHA-256 8505217e945b180264d5d84eb84729ac1d1e7a20d4c0e931a63c4e844922a8c3
  Python 3.12: passed; evidence sha256:a3c17050c7e42d8ec8049e809b11a68b2f8ccb32e4191466268b1ac54a8cf03b; JSON SHA-256 e29b6d48d03dcda6830dd400a540c804e20d97337391ae2972db309161e0bb70
  Python 3.13: passed; evidence sha256:ab55274da980bcf1bc4508b34e5cb5a996c869e2646f15bb15601f6af6517aea; JSON SHA-256 b8610b8fc39f8b9de69f64f84d49fa28eb0d31bf958ec4412a7e0091ea3f9077

Skill runtime sync check: passed
Deterministic ZIP check: passed
Codex Skill validator: passed
Release content gate: passed; release archives reject bundled `.tgz` files and unpinned Trellis install specs
Distribution clean-install CI matrix: 6 cells implemented; remote execution pending
Graph adapter qualification digest: sha256:211fc5fc7c72bc68447ed9c632c37223018c1afef3f77f7e9d5bdf297db6da1e
Trellis compatibility digest: sha256:fd3601e3507f8e2befe914e94afff04c07dedfb55d30417d3b35370bbfacf235
Backend qualification digest: sha256:69117a88996d30378c41101fd2f9dae5f37d21fa3a3a2bdd25f72ceebb08b46a
Trellis compatibility JSON SHA-256: 489a9d356cc394ba597aa2420cce8ac37f7e4902da5304896058a86f56027a92
Backend qualification JSON SHA-256: 4eb266ccd3a72f7c6caa4df7860d0e9beed4b1c2529ac896ab80a893b3906a35
Candidate-revision safety packet: not generated for the dirty worktree; M1-13 remains open
```

三個 skip 分別是兩個需要 Windows symlink 權限的測試與一個 POSIX-only 路徑。最新版 Ruff 對整個 repository 仍會列出既有檔案的格式／風格建議；CI 沒有全域 Ruff gate，本輪只檢查新增範圍，不應為收尾重排整個 repository。
受控 performance evidence 已記錄完整平台與 storage identity，但目前沒有同 identity baseline，因此只證明 absolute gate，不能聲稱相對退化檢查已通過。本機 candidate-bound changed-safety 與安全封包已通過；關閉 M1-13 前，仍需由最終候選 SHA 完成遠端 repository 與 distribution matrices。

## 主要文件

- 專案說明：`README.md`（英文主入口）與 `README.zh-TW.md`（繁中）
- Skill 主流程：`wish-builder/SKILL.md`
- Artifact contract：`wish-builder/references/artifact-contracts.md`
- Trellis／backend 邊界：`wish-builder/references/tool-bridges.md`
- 執行與恢復：`wish-builder/references/execution.md`
- 正式計畫：`C:\Users\chonk\.gstack\projects\new-chat\ceo-plans\2026-08-16-wish-builder.md`

## 接手順序

1. 先讀本文件、README、Skill 與正式計畫，不要從舊的 D44-D50 問題恢復；使用者已明確放棄那些過細決策。
2. package source、Skill 或 README 如有變動，重跑完整 Python suite、standalone Skill tests、runtime／ZIP parity、官方 Skill validator，並重建 wheel／sdist 後執行 clean-install matrix。
3. 如要關閉 M1-13，先確認目前最終候選 SHA 的本機 safety evidence provenance，再完成第一次 GitHub repository 與 distribution Windows／Linux、Python 3.11／3.12／3.13 matrices；若候選內容再變動，必須從新 SHA 重建相同證據。
4. 如要開放 Pi、Oh My Pi 或 Codex backend，先補齊對應 OS cell 的 active cancellation、當機重啟 reconcile、cleanup、平行重疊與平台矩陣證據；把證據根連到獨立可信的 attestation 或原始 event log，最後經人工審核才可更新 trust pin 與對應 `enabledForDispatch`。這不依賴 Trellis projection CAS。未來若要加入 `trellis + trellis`，不使用這些 Agent backend／OS cell；需另加 manifest schema，並驗證派工前准入、fencing、stop/reject 與並行寫入所有權。
5. 本輪候選與文件更新已獲授權；之後的新 commit、push、公開 repository、release 或 provider 憑證操作仍要另行取得使用者授權。

## 不可破壞的回歸規則

- Package source 是唯一 runtime authority；`wish-builder/scripts/wish_builder/` 必須由同步腳本產生。
- Trellis graph 有實質變動時，原 Gate B 必須自動失效。
- 完成順序不得決定 promotion 順序。
- Effect outcome 為 `unknown` 時必須 fail closed；不得猜測或重新派工。
- Recovery 只可從完整 verified replay 投影 pending dispatch requests。
- 不得讓 Trellis 和 Wish Builder 同時 dispatch 同一個 frozen graph。
