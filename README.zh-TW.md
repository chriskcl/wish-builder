# Wish Builder

[English](README.md) | 繁體中文

把一句產品想法，變成經過審閱、可以追蹤，也能讓 Agent 接手推進的軟體專案。

Wish Builder 是一套給 Codex 使用的 Skill，適合那些不能只靠一句提示就直接開始寫程式的專案。你先說明大方向，[gstack](https://github.com/garrytan/gstack) 協助整理產品與工程方案，由人確認產品範圍和架構；之後再由 [Trellis](https://github.com/mindfold-ai/Trellis) 建立可編輯的任務與依賴。Wish Builder 讀取並檢查任務圖，鎖定批准的版本，再監督後續執行。

它不會另外建立第二套任務拆分器或任務資料庫。可編輯的任務圖屬於 Trellis；批准後的執行快照，以及約束 Agent 的規則，屬於 Wish Builder。

> **目前狀態：** 開發預覽版（`0.1.0.dev0`）。本機控制流程、不可變執行快照檢查、遇到不確定情況就停止的准入規則、Journal 與恢復邊界、Git adapter，以及 Wish Builder 對官方 Trellis `0.6.15` 的匯入／投影 bridge 都已有實作。包含 Git 變更途中當機情況的完整本機生命週期，已使用受控 subprocess worker 通過端到端測試。
>
> 真實派工仍然關閉，因為 Pi、Oh My Pi 和 Codex 的六個 backend／OS cell，都還沒有完整的真實資格記錄。目前唯一留下的持久真實證據，是 Windows 上 Pi 的啟動和 handshake 檢查，而且沒有送出 model turn；Windows 上的 Oh My Pi live probe 需要已設定的 model 和 provider credential，但本輪沒有要求或使用 credential，因此該 cell 只能記為 credential-blocked。Codex 和其餘 cell 只有 deterministic fixture 或不完整的資格記錄。active cancellation、當機重啟後不重送的 reconcile、cleanup、平行重疊和平台證據仍不完整，因此所有 cell 都維持 `enabledForDispatch=false`。官方 Trellis `0.6.15` 也沒有跨程序 compare-and-swap（CAS），所以投影採單一寫入者並在衝突時停止。Agent 派工和 Trellis 投影是分開的：worker 只寫隔離 Git worktree 和 Journal，之後由單一 writer 把結果投影回 Trellis。GitHub repository 仍是 private，也尚未發布 release；程式碼採 GPL-3.0-only 授權。

## 為什麼需要它

一句簡短的產品想法很適合當起點，卻不足以安全地安排多個開發 Agent。沒有清楚流程時，常見情況是需求還沒定好就開始寫程式、幾個 Agent 同時改到相同檔案，或做到一半才發現架構需要重來。

Wish Builder 把工作維持在固定順序：

1. 修改產品程式碼之前，先把產品方向說清楚。
2. 由人批准範圍與架構。
3. 由 Trellis 把批准的文件整理成小任務和明確依賴。
4. 派工之前，先驗證並鎖定一個確切的任務圖版本。
5. 讓互不衝突的工作平行進行，再按固定順序測試、審閱和合併。

目的不是把人完全拿掉，而是把人的注意力留給產品和架構決定，範圍清楚的工程工作才交給 Agent。

## 運作方式

```text
┌────────────────────────────────────────┐
│ 你提出產品方向                         │
└───────────────────┬────────────────────┘
                    │
                    v
┌────────────────────────────────────────┐
│ gstack 在獨立子工作階段審閱            │
└───────────────────┬────────────────────┘
                    │
                    v
┌────────────────────────────────────────┐
│ Wish Builder 集中整理重要決定          │
└───────────────────┬────────────────────┘
                    │
                    v
┌────────────────────────────────────────┐
│ Gate A：批准產品範圍與架構             │
└───────────────┬────────────────────────┘
         通過   │       要改 → 退回 gstack
                v
┌────────────────────────────────────────┐
│ Trellis 建立任務與依賴                 │<────┐
└───────────────────┬────────────────────┘     │
                    │                          │
                    v                          │
┌────────────────────────────────────────┐     │
│ Wish Builder 匯入並驗證                │─────┘
└───────────────────┬────────────────────┘  有問題 → 退回 Trellis
                    │
                    v
┌────────────────────────────────────────┐
│ Gate B：批准任務圖投影與衍生摘要       │
└───────────────┬────────────────────────┘
         通過   │       要改 → 退回 Trellis
                v
┌────────────────────────────────────────┐
│ 鎖定、派工、驗證與恢復                 │
└───────────────────┬────────────────────┘
                    │
                    v
┌────────────────────────────────────────┐
│ 測試、審閱、合併與歸檔                 │
└────────────────────────────────────────┘
```

這是完整流程的目標設計。目前預覽版已包含組裝完成的本機生命週期與當機恢復路徑，並使用受控 subprocess worker 通過驗證。Trellis 相容性已通過匯入和單一寫入者投影檢查；另一份 backend 資格記錄仍會在真實派工前擋下所有 backend。啟動或 handshake 檢查不等於 model turn，目前沒有任何 backend／OS cell 完成取消、當機重啟 reconcile、cleanup、平行重疊和平台證據的完整驗證。

規劃階段通常依序使用 `office-hours`、`plan-ceo-review`、`plan-eng-review`；產品有畫面時，再加上 `plan-design-review`。每個 review 都在獨立、非互動的子工作階段執行。子工作階段只暫用 review 明確標示的推薦選項，讓審閱可以完成，再交回實際結果、其他選擇、日後是否容易修改和技術原因。純工程且容易撤回的選擇可以自動記錄；產品、架構、成本、安全或其他重要決定，會改寫成白話後集中放進 Gate A。gstack 的推薦只是建議，不代表人已批准。子工作階段若直接向使用者提問，或交回的決策資料不完整，該次 review 會停止。

Gate A 是由人批准「要做什麼」和「主要部分如何配合」的決定點。

接著由 Trellis 準備候選任務圖。Wish Builder 檢查依賴、負責檔案、驗收指令和任務大小。發現問題就退回 Trellis 修改，不會自己另建一份任務清單。檢查時使用的任務圖快照、revision 摘要和 manifest 都是 Wish Builder 衍生契約，不是 Trellis 官方 API。

Gate B 批准從一次穩定 Trellis task-record 讀取投影出的 material graph，以及 Wish Builder 衍生的任務圖和 execution manifest 摘要、scheduler、worker backend 和權限。Wish Builder 衍生的 task-record revision digest 只作 provenance；只要 canonical graph digest 不變，status、progress 和其他生命週期變動不會讓 Gate B 失效。Trellis 任務圖只要有實質改動，舊批准就會失效。Gate B 通過前，Wish Builder 不會修改產品程式碼，也不會派出實作 Agent。

## Trellis 與 Wish Builder 的分工

| Trellis 負責 | Wish Builder 負責 |
| --- | --- |
| 建立與拆分任務 | 驗證匯入的任務圖 |
| 編輯任務依賴 | Gate B 與內容摘要 |
| 任務內容和生命週期 | 不可變的執行快照 |
| Implement、Check、Finish | 准入、fencing、Journal 和恢復 |
| 任務歷史與歸檔 | 結果驗證與合併准入 |

這個專案不包含 PRD-to-task 生成器、任務 CRUD、另一個看板或第二套任務資料庫。

## 排程與 Agent backend

整體設計定義了兩種互斥的排程模式：

| `scheduler_mode` | `worker_backend` | 預定分工 | 目前 M1 狀態 |
| --- | --- | --- | --- |
| `trellis` | `trellis` | Trellis 排程同層任務；Wish Builder 驗證和監督 | 關閉：Trellis scheduler 尚未有通過資格驗證的派工前准入與 fencing 整合；`0.6.15` 也沒有跨程序 CAS |
| `wish_builder` | `pi`、`oh_my_pi` 或 `codex` | Wish Builder 按凍結任務圖派工到隔離 worktree；另一個單一 writer 稍後把 Journal 結果投影到 Trellis | 本機生命週期與當機恢復已驗證；backend／OS 資格證據未完成，因此關閉派工 |

M1 目前的 Python 控制層只接受 `scheduler_mode=wish_builder`。每次執行只選一種 backend；如果該 backend 無法使用、尚未取得派工資格，或所選組合不受支援，流程會直接停止，不會偷偷換成另一種。使用目前隨附的資格記錄時，`wishctl run` 會回傳 `dispatch_not_qualified`，不會派出 Agent。

未來實作 Trellis scheduler 時，`GraphIndex` 仍只會是驗證和恢復索引，不會變成第二個 dispatcher。

目前對 backend 的判定刻意保守：

| Backend | Windows 證據 | Linux 證據 | 正式派工 |
| --- | --- | --- | --- |
| Codex | deterministic fixture；仍需完整真實資格驗證 | deterministic fixture；仍需完整真實資格驗證 | 關閉 |
| Pi | 只有啟動和 handshake，沒有 model turn | deterministic fixture；仍需完整真實資格驗證 | 關閉 |
| Oh My Pi | live turn 受阻：需要已設定的 model 和 provider credential | deterministic fixture；仍需完整真實資格驗證 | 關閉 |

目前沒有任何 backend／OS cell 完成 active cancellation、當機重啟後不重送的 reconcile、cleanup、平行重疊和目標平台所需的完整資格證據，因此六個 provider／OS 組合仍是 `enabledForDispatch=false`。Trellis 相容性和 backend 資格是兩份獨立記錄：前者綁定凍結任務圖和 projection adapter，後者記錄 Agent cell 證據。active `wish_builder` 派工必須有已開放、且綁定批准 Trellis compatibility digest 的 backend cell；因 worker 不寫 Trellis，所以不把 projection CAS 當成派工條件。未來由 Trellis 排程的模式不使用 Agent backend／OS cell，但要有新版 manifest schema、派工前准入、fencing、stop/reject 和並行寫入所有權資格。Claude Code 和 macOS 已延後，等前三個 backend 與 Windows／Linux 矩陣穩定後再處理。

Trellis 相容性和 backend 派工資格是兩份不同契約：

- [`wish_builder/compatibility/trellis-0.6.15.json`](wish_builder/compatibility/trellis-0.6.15.json) 驗證官方 `@mindfoldhq/trellis@0.6.15` 與 `@mindfoldhq/trellis-core@0.6.15`，只涵蓋文件所述的匯入和單一寫入者投影邊界。
- [`wish_builder/compatibility/backend-qualification-0.6.15.json`](wish_builder/compatibility/backend-qualification-0.6.15.json) 記錄各 backend／OS 的派工證據；目前所有 cell 都是關閉狀態。

這項整合不可安裝或解析 `@latest`。`0.7.0-dev.2` 是後來從 Wish Builder 撤回的本機測試 fixture，從未是官方 Trellis release，也不受支援。官方 Trellis `0.6.15` 沒有可靠的跨程序 CAS；M1 因此同一時間只允許一個投影寫入者，只接受穩定的 task record 讀取，寫入前核對預期 SHA-256，寫入後再驗證 SHA-256 和內容，遇到衝突或結果不明就停止。這些 digest 檢查是投影完整性保護，不是 CAS，也不是 Agent 派工鎖。backend worker 只寫隔離 Git worktree 和 Journal。另一個 Trellis scheduler 模式還需要通過資格驗證的派工前准入、fencing 和並行寫入所有權。

## 已實作內容

目前 repository 包含：

- 嚴格的輸入格式和固定、可重現的錯誤訊息；
- 從官方 Trellis `0.6.15` task record 衍生確定性的 Wish Builder 任務圖快照，再產生 manifest v2；
- 透過官方 Core `loadTaskRecord`／`writeTaskRecord`，把單一寫入者的生命週期結果投影到權威 Trellis repository，並執行穩定讀取、寫前／寫後摘要檢查；寫前摘要衝突後不會自動重試；
- 依賴、負責檔案、ready set 和需求追蹤檢查；
- 與內容摘要綁定的 Gate 決定，後續修改會讓舊批准失效；
- append-only Journal、lease、epoch、fencing、checkpoint、replay 和 `GraphIndex` 重建；
- 總控元件、隔離的 attempt worktree、固定 promotion 順序，以及使用模擬 subprocess 的端到端測試；
- 目標分支前進前，會在實際 promotion candidate 裡執行一般專案的驗收指令；
- subprocess 隔離、輸出限制、timeout 與 fail-closed 恢復；
- Git staging、promotion、cleanup、quarantine 和 trace/export service；
- Python package 與獨立 Skill runtime 同步，以及可重現的開發版 ZIP；
- contracts、排程、恢復、Git effects、打包和受控效能的本機測試。

這些元件已有實作和測試。組裝完成的本機生命週期，包括 Git 變更途中當機後的恢復，已使用受控 subprocess worker 通過端到端測試。它位於受保護的 `wishctl run` 入口後面；不過內附的 backend 記錄會在真實派工前擋下命令。剩餘的 backend 工作，是把 smoke 結果整理成完整、可按內容摘要核對的取消、當機重啟 reconcile、cleanup、平行重疊和各平台資格證據。

真實 Issue、Pull Request、託管平台、憑證、background supervisor 和正式部署 adapter 都不在目前實作內。

## 安裝開發預覽版

### 使用前準備

- Python 3.11 或更新版本
- Git
- Node.js 18.17 或更新版本
- 支援 Skill 的 Codex
- gstack
- `@mindfoldhq/trellis@0.6.15`
- 供 bridge 使用、已驗證的本機 `@mindfoldhq/trellis-core@0.6.15` SDK

Python runtime 本身不依賴第三方 package。Wish Builder 會在開始前列出缺少的工具；未經批准，它不會自行安裝全域工具、登入帳號或修改 repository 設定。

Trellis CLI 必須按確切版本安裝：

```bash
npm install -g @mindfoldhq/trellis@0.6.15
```

Core bridge 可讀取解壓後的 `@mindfoldhq/trellis-core@0.6.15` package 目錄，或經過驗證的官方 npm tarball。tarball 只是本機驗證輸入，不會被打包進 Wish Builder 發行內容。不可改用 `@latest` 或其他預發布版本。

### 從本機 ZIP 安裝

Repository 內的 [`wish-builder-skill.zip`](wish-builder-skill.zip) 已和目前 runtime 同步，可供本機評估，但它不是正式發布版本。

Windows PowerShell：

```powershell
Expand-Archive .\wish-builder-skill.zip -DestinationPath "$env:USERPROFILE\.codex\skills"
```

macOS 或 Linux：

```bash
mkdir -p ~/.codex/skills
unzip wish-builder-skill.zip -d ~/.codex/skills
```

安裝後應該看到：

```text
~/.codex/skills/wish-builder/SKILL.md
```

目前 ZIP 的 SHA-256：

```text
974cd75b5fb2e5c454f1d642e7d40c6288718728b695cb28a5897586dbcf8b48
```

GitHub repository 目前仍是 private，所以還不能作為其他人的安裝來源。公開後，可以透過 Codex Skill installer 安裝 repository 內的 `wish-builder/` 目錄。

## 開始一個專案

在 Codex 開啟目標 Git repository，再給 Wish Builder 一個簡短方向：

```text
Use $wish-builder in this repository.

我想做一個給兩位室友使用的共同記帳工具。
第一版只在本機使用，不需要付款或銀行連接。
我批准產品、架構和任務圖後，你可以自行繼續。
只有工作超出批准範圍、需要高風險操作，
或準備正式部署時才停下來問我。
```

你不用一開始就寫完整規格。早期審閱會逐步找出使用者、問題、成功標準、範圍邊界和架構。目前預覽版適合驗證規劃與 manifest 準備流程，不應被理解成已能全程無人看管地正式派工。

## 仍需要人決定的地方

| 決定點 | 何時出現 | 你要批准什麼 |
| --- | --- | --- |
| Setup gate | 需要工具、登入或 repository 變更時 | 安裝、初始化、認證或設定 |
| Gate A | Trellis 準備任務圖前 | 產品目標、範圍、架構、資料與安全選擇 |
| Gate B | 派出實作 Agent 前 | 從 Trellis task records 投影出的 material graph、Wish Builder 衍生的任務圖與 manifest 摘要、scheduler、backend、測試、合併規則和權限 |
| Gate C | 正式部署前 | 部署位置、風險、檢查和回復方案 |
| 偏離決定 | 工作超出批准邊界時 | 是否修改範圍、架構、公開介面或安全邊界 |

願望不等於批准。Gate A 和 Gate B 都需要明確回覆「通過」，或列出要改的內容。

## 安全邊界

未取得額外批准時，Wish Builder 不會：

- 索取、保存或更換憑證；
- 花錢或修改帳單；
- 部署到正式環境；
- 刪除正式資料或降低存取控制；
- 執行無法回復的資料轉換；
- 改變已批准的產品方向、架構或公開介面；
- 讓 Trellis 和 Wish Builder 同時派發同一個任務圖。

一般的實作選擇和普通測試失敗由 coordinator 處理。範圍變更、高風險操作和無法排除的重複失敗，才會退回給人決定。

## 指令列檢查

`wishctl` 把重要流程規則變成可重複執行的檢查。執行時只使用 Python 標準函式庫。

| 指令 | 用途 |
| --- | --- |
| `validate` | 驗證批准、任務圖、檔案範圍、測試與恢復資料 |
| `ready` | 列出凍結任務圖中可開始且互不衝突的任務 |
| `drift` | 檢查修改檔案是否落在任務負責範圍內 |
| `trace` | 產生需求、任務和交付結果的追蹤文件 |
| `hash` | 計算 Gate 文件的 SHA-256 |
| `snapshot-trellis` | 從官方 Trellis `0.6.15` task record 衍生 Wish Builder 任務圖快照 |
| `import-trellis` | 把 Wish Builder 衍生的 Trellis 任務圖快照轉成 manifest v2 |
| `decide` | 把 direct CLI Gate 決定寫入 Journal |
| `resume` | 根據驗證過的恢復證明，恢復一項狀態不明的派工 |

範例：

```bash
python scripts/wishctl.py --help
python scripts/wishctl.py validate path/to/execution-manifest.json --stage planning
python scripts/wishctl.py snapshot-trellis <parent-task-id> --core-archive path/to/mindfoldhq-trellis-core-0.6.15.tgz --output trellis-graph.json
python scripts/wishctl.py import-trellis path/to/trellis-graph.json path/to/import-settings.json --output execution-manifest.json
```

安裝後的 Skill 也提供相同 runtime：`wish-builder/scripts/wishctl.py`。

## Repository 結構

```text
.
|-- README.md                     英文說明與使用方式
|-- README.zh-TW.md               繁中說明與使用方式
|-- pyproject.toml                Python package 與 wishctl 入口
|-- wish_builder/                 正式 Python 實作
|   |-- adapters/                 Trellis、process、storage 與 Git 邊界
|   |-- compatibility/            固定版本的 Trellis 相容性與 backend 資格證據
|   |-- contracts/                輸入與 artifact 格式
|   |-- kernel/                   DAG、Gate、狀態與 GraphIndex
|   |-- presentation/             trace 與 export 輸出
|   |-- processes/                coordinator 與 worker 執行
|   `-- services/                 Journal、恢復、cleanup 與 Git service
|-- wish-builder/                 可獨立安裝的 Codex Skill
|-- scripts/                      build、同步與 CI 檢查
|-- tests/                        unit、integration、fault 與效能測試
`-- wish-builder-skill.zip        可重現的開發版壓縮檔
```

完整執行規則在 [`wish-builder/SKILL.md`](wish-builder/SKILL.md)，artifact 格式和工具邊界則放在 [`wish-builder/references/`](wish-builder/references/)。

## 驗證狀態

M1 的發行門檻是一套完整、可以重跑，而且綁定單一 commit 的本機證據。實際 revision 記在 `local-m1-evidence-manifest.json`，不另外抄進 README，避免文件與證據不一致。

| 檢查 | 結果 |
| --- | --- |
| Repository 矩陣 | Windows／Linux × Python 3.11／3.12／3.13；每格 1,498 項，沒有 failure 或 error |
| 發行檔 clean install | Wheel 與 source archive 在六個 OS／Python cell 全部通過 |
| 官方 Trellis `0.6.15` | Windows 與 Linux 各通過 22 項 Node 和 7 項 Python 整合測試 |
| Branch coverage | contracts/kernel 95.395242%；services 91.638225%；adapters/processes/CLI 88.033012%；全部通過門檻 |
| Safety mutation | 16 項全部攔下；分數 100% |
| Controlled performance | 10 萬事件冷重播 p95 10.521 秒；checkpoint tail p95 6 ms；graph batch p99 1.118 秒；記憶體峰值 49,668,096 bytes |
| Codex Skill 結構驗證、runtime parity 與確定性 ZIP | 通過 |

M1 發行以綁定單一 commit、可以重跑的本機證據為準。`scripts/local_evidence_packet.py` 會驗證六個 repository cell、六個 clean-install cell、兩個 Trellis cell，以及 coverage、mutation、safety、performance 和確定性發行檔，最後寫出帶有 `provenance_kind: local` 的 canonical manifest。`scripts/ci_local_release.py` 會從原始證據重建同一份 manifest，完全相符才會建立含 checksum 的發行檔；若出現 GitHub workflow ID、job result 或其他 CI provenance，會直接拒絕。

這個預覽版不再把 GitHub Actions 當成 M1 發行門檻。最近一次 hosted run 因 repository 帳戶的付款或 spending limit 問題，在取得 runner 之前就停止；不能把它描述成通過。這項調整只改變發行證據來源，不會替任何 backend 完成資格驗證，也不會開放真實 Agent 派工。

本機主要測試指令：

```powershell
.\.venv\Scripts\python.exe scripts\ci_test_suite.py --exclude-package performance
.\.venv\Scripts\python.exe scripts\ci_test_suite.py --only-package performance
.\.venv\Scripts\python.exe wish-builder\scripts\test_wishctl.py
```

完成某個 commit 的本機原始矩陣證據後，可用以下指令建立並再次驗證發行資料：

```powershell
uv run --locked --python 3.13 python scripts\local_evidence_packet.py `
  --evidence-root <evidence-root> --candidate-revision <commit-sha> `
  --safety-base-ref <base-ref> --output <manifest.json> `
  --digest-output <manifest.sha256>

uv run --locked --python 3.13 python scripts\ci_local_release.py `
  --repository-root . --evidence-root <evidence-root> `
  --safety-base-ref <base-ref> --distribution-root <distribution-root> `
  --manifest <manifest.json> --manifest-digest <manifest.sha256> `
  --output-dir <release-assets> --revision <commit-sha> `
  --version 0.1.0.dev0 --tag v0.1.0.dev0
```

## 開放真實派工前

- 使用真實專案 backend 驗證已組裝的生命週期，包括 Git 變更前後的取消與恢復。
- 補齊取消、當機重啟 reconcile、cleanup、平行重疊與平台證據，再逐一開放 backend cell。
- 補上所選流程需要的真實 Issue、Pull Request 和託管平台 adapter。
- 完成一個公開案例，從一句產品願望走到通過審閱並合併的改動。

## 參與開發

提出修改時，先說清楚它解決的實際問題。流程改動應一起更新 Skill、相關參考文件和測試；修改 `wishctl` 時，應加入一個能重現問題的小型測試。

不要在沒有同等替代措施的情況下，移除批准 Gate、負責檔案檢查、fencing 規則或 fail-closed 恢復行為。

## 授權

Wish Builder 採用 [GNU General Public License v3.0 only](LICENSE) 授權。隨附與開發工具的 notices 請見 [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md)。
