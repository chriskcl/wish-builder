# Wish Builder Project Handoff

交接時間：2026-08-30（Asia/Macau）

## 一句話現況

Wish Builder 的本機 M1 功能、M1-13 驗證與 release verifier 已落地：Trellis 建立和維護
可編輯任務圖；Wish Builder 匯入、驗證並鎖定人已批准的 material graph，再負責准入、
派工監督、結果驗證、Journal 和崩潰恢復。唯一支援的 Trellis 基線是官方 `0.6.15`；
`0.7.0-dev.2` 是已撤回的本機測試 fixture，從未是官方 Trellis release。**本地測試已通過，
依使用者決定足以完成 M1。** GitHub Actions 因預算用完而不執行，不聲稱 CI 通過或失敗。
`Codex / Windows` 已依完整 live evidence、獨立核對與人工批准完成本地正式發布，可在並行度
1 或 2 派工；其 detached provider provenance 不是 OpenAI 簽署的 attestation。其餘五個
backend／OS cell 仍關閉。

## Repository 狀態

| 項目 | 值 |
| --- | --- |
| Repository | `C:\Users\chonk\Documents\Codex\2026-08-15\new-chat\outputs` |
| Branch | `release/codex-windows-qualification`（由 `main` 建立） |
| Current branch base HEAD | `fd3296ed1f8d85e9a1347eb1e2dcdf611ec62720` (`docs: simplify Trellis README guidance [skip ci]`) |
| M1 candidate revision | 本文件所在 commit；接手時執行 `git rev-parse HEAD` 取得，不在 commit 內自我引用 SHA |
| 工作樹 | 本輪候選已提交並推送；審核時工作樹乾淨，HEAD 與 `origin/release/codex-windows-qualification` 一致 |
| Remote | `origin = https://github.com/chriskcl/wish-builder.git`；repository 已公開，`v0.1.0.dev1` prerelease 已發布 |
| Python package | `wish-builder 0.1.0.dev1`, Python `>=3.11` |
| License | `GPL-3.0-only` |
| Skill ZIP | `wish-builder-skill.zip` |
| Skill ZIP SHA-256 | `8a9887281f5c1b60d11fe4231e298c09284f2d0a0fd9fb3b77a8c8dadeb1ed1a` |
| Skill ZIP 大小 | `556,382` bytes |

`changed-lines.json`、`mutation-report.json` 和 `safety-evidence.json` 是本機 evidence，不屬於提交內容；不要把它們加入 commit。本輪 tracked 候選已推送到 `origin/release/codex-windows-qualification`；後續以 `git status --branch`、remote ref 和 `gh pr view` 核對實際狀態。

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
- M1-13 三層 coverage floor、16 項 targeted safety mutation、coverage＋mutation 聯合證據、Windows／Linux performance evidence，以及一次建置、六格共用同一 artifact 的 distribution clean-install matrix。
- 本機 release verifier 會從原始證據重建 canonical manifest，再驗證 wheel、sdist、兩份 Skill ZIP 與 distribution evidence；manifest 使用 `provenance_kind: local`，不接受 workflow ID、job results 或其他 CI 身分欄位。原有 CI verifier 保留為可選路徑。
- `Codex / Windows` 在官方 `@openai/codex@0.149.0` 上完成 full turn、active
  cancellation、crash/reconcile without redelivery、cleanup 與兩個 disjoint sibling
  overlap；獨立核對通過後，由 fail-closed publisher 保存 evidence、publication receipt、
  bundled record 和 compiled trust pin。最大並行度為 2。

## 尚未完成或未開放

- Pi／Windows、Pi／Linux、Oh My Pi／Windows、Oh My Pi／Linux 和 Codex／Linux 五個 cell
  仍是 `enabledForDispatch: false`。Windows Pi 只有啟動／handshake 證據，沒有 model turn；
  Windows Oh My Pi 的 live turn 需要已設定的 model 和 provider credential，本輪沒有要求或
  使用 credential，因此只能記為 `blocked_credentials`。開放任一 cell 前，必須補齊完整
  live evidence、保存可核對的原始 event log，經獨立核對和人工批准，再透過 publisher 更新
  trust pin；不可手改資格 JSON。
- 官方 Trellis `0.6.15` 沒有可靠的跨程序 CAS，因此 projection 維持單一 writer，寫前後
  核對 digest，衝突或結果不明時 fail closed。backend worker 只寫隔離 Git worktree 和
  Journal，不寫 Trellis；Agent 派工和 Trellis projection 是兩條獨立准入線。
  `Codex / Windows` 的派工資格不依賴 projection CAS。
- Claude Code 和 macOS 明確延後，不屬於 v1。
- 不含 AI PRD-to-task decomposer、任務 CRUD、另一套 task DB／看板或第二個 scheduler。
- M1 只保留 Python 控制層與 Trellis／backend 整合；其他工具鏈不在目前範圍。
- 不含真實 GitHub adapter、provider 憑證、sandbox、background supervisor／broker、cockpit 和正式部署。
- M1-13 已依「本地測試通過」規則完成。GitHub Actions 因預算用完而不執行，也沒有 CI 結果可宣稱。
- `local_evidence_packet.py` 和本機 release verifier 保留為可選的嚴格發行工具，不再是 M1 完成門檻。
- 已採 GPL-3.0-only 並加入第三方 notices；repository 已公開，`v0.1.0.dev1` prerelease 已發布。

## 驗證基線

目前 M1 判定為「本地測試通過」。以下歷史段落保留較細的本機測量資料供比較；本機 evidence JSON 不加入 repository。

```text
2026-08-30 Codex/Windows local qualification publication (source revision fd3296ed1f8d85e9a1347eb1e2dcdf611ec62720):
  Independent evidence audit: PASS; 52 passed; 1 Windows symlink-permission skip
  Post-publication qualification/admission focus: 68 passed; 1 Windows symlink-permission skip; 59 subtests passed
  Fresh full local suite: 1,527 run including 16 performance tests; OK; 3 platform-specific skips
  Installed standalone Skill: 13 run; OK

2026-08-30 local non-performance candidate matrix (revision 9793ff1c86089c59115f4406a015c3abec8d6bce):
  Windows/Linux x Python 3.11/3.12/3.13: 1,498 tests per cell; status passed; 0 failures; 0 errors
  Allowed skips: Windows 9 per cell; Linux 13 per cell

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
Current Skill ZIP SHA-256: 8a9887281f5c1b60d11fe4231e298c09284f2d0a0fd9fb3b77a8c8dadeb1ed1a; 556,382 bytes
Codex Skill validator: passed
Release content gate: passed; release archives reject bundled `.tgz` files and unpinned Trellis install specs
Distribution clean-install matrix: 6 cells implemented; local fixed-revision evidence is authoritative for M1
Graph adapter qualification digest: sha256:211fc5fc7c72bc68447ed9c632c37223018c1afef3f77f7e9d5bdf297db6da1e
Trellis compatibility digest: sha256:fd3601e3507f8e2befe914e94afff04c07dedfb55d30417d3b35370bbfacf235
Backend qualification digest: sha256:9f6606ef8a872b1eadfc1c34451c99c5cd6bc5b49d704c30d3b56d7fb8a171fc
Trellis compatibility JSON SHA-256: 489a9d356cc394ba597aa2420cce8ac37f7e4902da5304896058a86f56027a92
Backend qualification JSON SHA-256: b7744f9e9a4189518e934eb84cc7bccc7a6f88bea2bfe9eb58b1e5dab677b1c4
Codex/Windows candidate artifact: sha256:8d6cf3545a978ea1221df250818ab54e86ab521d698ed1e4c1c6076119dc58a6
Codex/Windows evidence inventory: sha256:8a645308959b79e9d95fdacc79fbaa4a31bb5598ea564f43325cedebd6a6887d
Codex/Windows event log: sha256:a96c043cfe94a3ccfef9381c35c4ae547ffcee3f88f4ab683a600f17a06d151c
Codex/Windows publication receipt: sha256:d59bfd97595e2f1f9cddf7e1fc2999ec8b4c9a963c3e0834388e492e75c27d8e
Qualification source revision: fd3296ed1f8d85e9a1347eb1e2dcdf611ec62720
Candidate-revision safety packet: optional under the current M1 policy
```

三個 skip 分別是兩個需要 Windows symlink 權限的測試與一個 POSIX-only 路徑。最新版 Ruff 對整個 repository 仍會列出既有檔案的格式／風格建議；CI 沒有全域 Ruff gate，本輪只檢查新增範圍，不應為收尾重排整個 repository。
受控 performance evidence 會記錄完整平台與 storage identity；沒有同 identity baseline 時，只能證明 absolute gate，不能聲稱相對退化檢查已通過。這些較嚴格的 evidence matrix 現在是可選的發行驗證，不阻擋 M1 完成。

## 主要文件

- 專案說明：`README.md`（英文主入口）與 `README.zh-TW.md`（繁中）
- Skill 主流程：`wish-builder/SKILL.md`
- Artifact contract：`wish-builder/references/artifact-contracts.md`
- Trellis／backend 邊界：`wish-builder/references/tool-bridges.md`
- 執行與恢復：`wish-builder/references/execution.md`
- 正式計畫：`C:\Users\chonk\.gstack\projects\new-chat\ceo-plans\2026-08-16-wish-builder.md`

## 接手順序

1. 先讀本文件、README、Skill 與正式計畫，不要從舊的 D44-D50 問題恢復；使用者已明確放棄那些過細決策。
2. package source 或 Skill 如有變動，重跑相關本地測試、standalone Skill tests 與 runtime parity；只有發行檔內容改變時才需要重建 wheel、sdist 和 Skill ZIP。純文件改動執行格式與連結檢查即可。
3. M1-13 已關閉。GitHub Actions 因預算不執行；除非日後另有預算和明確需求，不要把 hosted CI 加回完成門檻。完整 local evidence packet 只在需要可重建發行資料時執行。
4. `Codex / Windows` 已開放並行度 1-2；不要提高到 3。若要開放其餘五個 cell，先補齊
   對應 OS 的完整資格證據，保存原始 event log，經獨立核對與人工批准後使用 publisher
   發布，不可手改資格 JSON。這不依賴 Trellis projection CAS。未來若要加入
   `trellis + trellis`，不使用這些 Agent backend／OS cell；需另加 manifest schema，並驗證
   派工前准入、fencing、stop/reject 與並行寫入所有權。
5. 本輪候選與文件更新已獲授權；之後的新 commit、push、公開 repository、release 或 provider 憑證操作仍要另行取得使用者授權。

## 不可破壞的回歸規則

- Package source 是唯一 runtime authority；`wish-builder/scripts/wish_builder/` 必須由同步腳本產生。
- Trellis graph 有實質變動時，原 Gate B 必須自動失效。
- 完成順序不得決定 promotion 順序。
- Effect outcome 為 `unknown` 時必須 fail closed；不得猜測或重新派工。
- Recovery 只可從完整 verified replay 投影 pending dispatch requests。
- 不得讓 Trellis 和 Wish Builder 同時 dispatch 同一個 frozen graph。
