# Smart Axiata – MSTR → Power BI/Fabric Migration – Technical Context

22 Oct 2026 · Greg · Master copy: https://claude.ai/artifact/6UT1sRkJwTeVEhDXM7dc3v (this file is a snapshot; the repo changes of 22 Oct – build spec links, LARGE_MODEL – are not yet in the master copy)

## Engagement at a glance

SBI rebuilds Smart Axiata's 151 MicroStrategy dashboards as Power BI reports on Microsoft Fabric. It is a reporting-layer migration only: Smart's data warehouse and dimensional models stay as they are (Unverified · discovery notes 16 Jul, consistent in every later meeting).

The work runs as a Phase 0 foundation – target architecture, security and access model – followed by 3-week build sprints. Reports ship in two release groups (P1, P2); each passes SBI peer review, then UAT by Smart's IT team (Unverified · clarification notes 16 Sep).

Status on 21 Oct 2026: project won, Phase 0 readiness under way on Smart's side – see Prerequisites.

**How to read this doc.** Every fact carries its basis. **Confirmed** = client in writing (email, inventory file, scoping answers) or confirmed by Greg on site. **Unverified** = meeting notes not yet confirmed in writing – check before building on them. **SBI** = our own decision or assumption. Financial and contractual terms are left out on purpose.

## Scope

SBI rebuilds all 151 dashboards end to end – one report per dashboard, on semantic models shared where dashboards use the same fact tables – and builds the Fabric/Power BI foundation they run on. Smart owns the data platform, infrastructure access, validation and end-user training. Scope below is taken from proposal V2.0 and the 16 Sep clarifications (SBI); check it line by line against the signed SOW at kickoff.

**SBI delivers – Foundation**

- Fabric tenant settings, capacity setup and capacity administration.
- Gateway configuration and connectivity to ClickHouse, PostgreSQL and Cloudera, on the server and service accounts Smart provides.
- Workspace, semantic model, report and distribution architecture, including a self-service governance model for power users.
- Security and access model design: workspace access, permissions, sharing.
- Version control and dev/test/prod deployment pipelines.
- Report standards, templates and the QA process.
- Capacity sizing validated against the largest cubes, with measured evidence.
- Documents: Target Architecture, Migration & Cutover Plan, Security & Access Framework, capacity sizing report.

**SBI delivers – Migration**

- 151 reports (1,178 pages, 3,065 visuals) on consolidated semantic models, each reconciled against its MicroStrategy original.
- Custom map visual for Cambodian administrative divisions, and replication of the site count metric.
- Subscription rebuild across PDF, Excel and email-body formats.
- Excel connectivity (XMLA / Analyze in Excel) to replace the MicroStrategy Excel add-in.
- Per-report documentation (sources, metrics, definitions), platform documentation and an admin troubleshooting guide (Unverified · clarification notes 16 Sep).
- UAT support, cutover and a hypercare window.
- Technical team enablement: Fabric governance training plus a developer handover on gateway, deployment, scheduling and permissions – not full report-building training (Unverified · clarification notes 16 Sep).

**Smart owns**

- Gateway server, service accounts and network access to the three sources.
- Final inventory and P1/P2 order, confirmed before S1.
- UAT through its IT team, within sprint windows.
- End-user training.
- The development freeze on MicroStrategy for the whole migration.
- MicroStrategy decommission: metadata request to the vendor, local storage of dashboards, backup VM.
- Row-level security implementation, unless it is moved into SBI scope.

**Out of scope**

- Any change to source systems, ETL pipelines or the data platform.
- Event-driven refresh on ETL completion – the ETL machine has no internet access, so this follows after migration.
- Mobile, embedding, and pixel-perfect output beyond distribution parity.
- AI/ML work, and any dashboard not on the confirmed inventory.
- Licence procurement.

## Current state – MicroStrategy estate

All 151 dashboards sit in one MicroStrategy project, consumed through Library and fed mostly by MTDI import cubes. Complexity is in the dataset layer, not the visuals: the heaviest dashboards carry 46–49 cubes each, among them Cluster Head (49) and Home Internet Performance (46 cubes, 185 visuals).

| Wave | Dashboards | Visuals |
| --- | --- | --- |
| P1 | 98 | 2,278 |
| P2 | 53 | 787 |
| Total | 151 | 3,065 |

The estate also holds 1,178 pages and 783 cube references (Confirmed · client inventory). Caveat: the inventory's "Total Visualization Count" column holds the smaller number, so we read it as pages – this reading is still unconfirmed. Workshop notes of 3 Sep cited 130 dashboards on 556 cubes; the inventory supersedes them, and the gap implies cube reuse not yet measured.

| Area | Today | Basis |
| --- | --- | --- |
| Platform | Workstation, Library and dashboards – current stack, not the legacy schema-first model; one project | Confirmed · on-site workshop 3 Sep |
| Datasets | Mostly one dedicated dataset per report, some reuse; heavy dashboards combine several MTDI cubes; queries mostly `SELECT *` or light transformation | Confirmed · on-site workshop 3 Sep |
| Schema objects | Schema-based OLAP cubes still exist but are a minority (~10% of dashboards) | Confirmed · workshop 3 Sep; share: Unverified · notes 28 Aug |
| Data sources | ClickHouse, PostgreSQL, Cloudera CDP (Impala, Hive), reached through an on-premises gateway; ETL machine has no internet access | Confirmed · workshop 3 Sep; discovery 16 Jul |
| Volumes | Largest cube 21 GB / 141M rows; five cubes above 10 GB | Unverified · notes 3 Sep; Confirmed · scoping answers |
| Refresh | Mostly time-triggered, ~90% daily and ~5% hourly; some cubes triggered at the end of the ETL flow | Confirmed · workshop 3 Sep; scoping answers |
| Visuals | Mostly tables, bar and trend charts, KPI cards; maps with custom shapes for Cambodian administrative divisions | Confirmed · on-site workshop 3 Sep |
| Distribution | A few subscriptions in several delivery formats; users query datasets from Excel through the MicroStrategy add-in | Confirmed · on-site workshop 3 Sep |
| Security | No security filters; restricted data is served by separate pre-filtered reports; access granted per dashboard; no mobile | Confirmed · on-site workshop 3 Sep |
| Users | ~200 users, ~50 peak concurrency, ~40 authors (scoping answers) versus 10–15 active users (notes 3 Sep) – unresolved | Conflicting |
| Telemetry | Platform Analytics collects usage telemetry | Confirmed · on-site workshop 3 Sep |
| Metadata | Held by the vendor, not accessible to Smart – the inventory is the scope of record | Confirmed · on-site workshop 3 Sep |
| Hosting | Managed cloud instance run by a third party | Unverified · discovery 16 Jul |
| Data governance | Only aggregated data may leave on-premise | Confirmed · scoping answers |
| Microsoft estate | M365 tenant in place, no Azure services | Unverified · discovery 16 Jul |

## Target architecture

Three on-premise sources feed the semantic models in Smart's Fabric tenant through a single gateway; users consume the reports in the browser, by subscription and from Excel (SBI · proposal V2.0; Confirmed · workshop 3 Sep).

**Architecture in text** (drawn as a diagram in the master copy):

1. Smart network, on-premise: ClickHouse, PostgreSQL and Cloudera CDP (Impala and Hive) → on-premises data gateway on a Windows VM inside Smart's network; SBI installs and configures it.
2. Gateway → Smart's Microsoft tenant, Fabric capacity (F64 proposed; SKU measured in Foundation, home region to confirm), carrying scheduled refresh and live queries.
3. Inside the capacity: workspaces dev → test → prod, promoted by deployment pipelines, holding the semantic models (shared on common fact tables; import, DirectQuery or dual per model) → 151 reports (1,178 pages, 3,065 visuals; custom map visual, site count metric). PBIP files sync with Git (provider to confirm).
4. Consumption: Power BI service in the browser (no mobile), subscriptions (PDF, Excel, email body), Excel through XMLA / Analyze in Excel. Access limited to office and VPN by Entra ID Conditional Access (proposed).

The gateway is the only path between Smart's network and Fabric: nothing refreshes until it is installed and its data sources are configured.

| Area | Design direction | Basis |
| --- | --- | --- |
| Capacity | F64 proposed over the estimator's F32: F32 caps a semantic model at 10 GB, below the largest cubes; F64 allows 25 GB. The SKU is confirmed by measurement in Foundation, and F SKUs resize without a rebuild | SBI · presales Q&A |
| Tenant and region | Fabric in Smart's existing tenant, not a second one. Fabric runs in Southeast Asia; check the tenant's home region, and use Multi-Geo only if needed (not supported with Premium Per User) | SBI · presales Q&A |
| Gateway | One on-premises data gateway on a Windows VM inside Smart's network, connected to ClickHouse, PostgreSQL and Cloudera (Impala, Hive) | Confirmed · email 21 Oct; SBI |
| Semantic models | Shared by dashboards with the same fact tables when the consolidation conditions hold (build spec); otherwise one per dashboard, consolidating its MTDI cubes into one governed model | SBI · proposal V2.0; Unverified · notes 16 Sep |
| Storage mode | Import by default; DirectQuery, dual mode or aggregations for the largest cubes | SBI · presales Q&A; Unverified · notes 28 Aug |
| Refresh | Time-based schedules, ~90% daily and ~5% hourly; incremental or table-level refresh per report; event-triggered refresh after the migration | Confirmed · workshop 3 Sep; Unverified · notes 16 Sep |
| Environments | Dev, test and prod workspaces promoted by deployment pipelines, which need capacity workspaces; reports and models kept as PBIP files in Git | Unverified · notes 16 Sep; SBI |
| Network access | Entra ID Conditional Access with named locations for office and VPN ranges; needs Entra ID P1, deployed in report-only mode first with break-glass accounts excluded. Not Private Link: it blocks PDF and PowerPoint export, which subscriptions need | SBI · presales Q&A |
| Access model | Designed at project start from today's per-dashboard grants; MFA, sensitivity labels and auditing come with the platform; RLS stays with Smart unless moved into scope | Unverified · notes 16 Sep and 28 Aug; SBI · proposal V2.0 |
| Excel | XMLA endpoint and Analyze in Excel replace the MicroStrategy add-in | SBI · proposal V2.0 |

## Migration approach and delivery plan

The plan runs eight 3-week sprints with Foundation in parallel from day one: P1 (98 dashboards) goes live at the end of S6, the full estate at the end of S7, and S8 is contingency and hypercare (SBI · proposal V2.0). The proposal assumed a 6 Oct start, so calendar dates are re-set at kickoff (Unverified · notes 13 Oct).

**Sprint plan in text** (drawn as a roadmap in the master copy):

| Workstream | Sprints | Milestone |
| --- | --- | --- |
| Foundation | S1 | Signed off end S1 |
| P1 build · 98 dashboards | S1–S5 | Complete end S5 |
| Subscriptions, Excel connectivity | S5 | – |
| UAT and defect fixes | S2–S6 | Each sprint's reports go to UAT during the next sprint |
| P1 cutover, go-live | S6 | P1 live end S6 |
| P2 build · 53 dashboards | S7 | Full estate end S7 |
| Team enablement | S7 | – |
| Contingency, hypercare | S8 | S8 also absorbs public holidays |

Foundation runs alongside the first build sprint, with development starting in Power BI Desktop, so platform readiness never blocks the build.

**AI-assisted build.** A migration factory generates version 1 of all 151 dashboards in one run – models as TMDL, reports as PBIR – with a list of decision flags per dashboard. The team then reviews and refines them sprint by sprint in P1/P2 order (SBI · Greg, 22 Oct). Details in the [build spec](docs/Migration_Factory_Build_Spec.md) (master copy: [artifact](https://claude.ai/artifact/W5bzpmyrDGtsmNJEh5ybR9)).

**Sequencing.** Priority first, complexity second. S1 takes the four heaviest dashboards (46–49 cubes each), the custom map visual and the site count metric, so the hardest semantic-model patterns are proven while there is runway. Each later sprint mixes one or two complex dashboards with simpler ones (SBI · proposal V2.0).

**Effort model.** Semantic model: 1 hour per cube. Report: 0.5 hour per visual, data validation included; 8 hours make a man-day. Capacity is 3 developers × 15 days = 45 man-days per sprint, minus public holidays, with 1 day per sprint for planning, review and deployment (SBI · proposal V2.0; clarification notes 16 Sep). These rates are SBI judgement, not delivery history: log actuals from S1 and recalibrate.

**Team.** A Tech Lead owns Foundation and standards and reviews all output. Three developers build – seniors on platform setup, juniors on repeatable report work – and a PM runs sprint planning and weekly or bi-weekly status (SBI · proposal V2.0). Staffing is still in progress (APAC staffing, Oct).

**Definition of done, per report.** Acceptance criteria are agreed with Smart before the project starts (Confirmed · email 21 Oct). The three agreed aspects:

1. Data matches the MicroStrategy original; a difference that is provably correct can be accepted.
2. The report is not distinctly different from today and follows company branding. No design standard exists, so objective criteria are agreed before S1.
3. Response time is no slower than today, measured manually in UAT against average execution times Smart provides.

Source for the three aspects: Confirmed · workshop 3 Sep; detail: Unverified · clarification notes 16 Sep.

**Flow of a report.** SBI peer review → evidence pack (screenshots plus side-by-side data export) → UAT by Smart's IT team within the sprint window, while the next sprint builds. Late validation moves go-live; it never compresses the build (SBI · proposal V2.0).

**Freeze and hypercare.** Smart freezes new MicroStrategy development for the whole migration, agreed before build starts (Confirmed · workshop 3 Sep). Hypercare follows each go-live and covers migration-related stability only, not data issues (Unverified · clarification notes 16 Sep); its length is set in the SOW.

## Prerequisites and client readiness

Two items gate Foundation as of 21 Oct: the gateway VM is ready but the gateway software is not installed, and service accounts and source connectivity are not in place (Confirmed · email 21 Oct). Smart has asked SBI to install the gateway and connect it to Power BI, and will help request the accounts. The migration factory adds three items: MicroStrategy API access, developer access to the sources and the connector drivers.

| Item | Owner | Status (22 Oct) | Needed by | Basis |
| --- | --- | --- | --- | --- |
| MicroStrategy REST API: read-only service account with the Use Library privilege, and network access to the Library API | Smart | In progress | Factory run | Confirmed in principle · Greg 22 Oct |
| Developer access to the three sources from Power BI Desktop (VPN or jump host), for validation pass 1 | Smart | Unknown | Factory run | SBI · build spec |
| Gateway VM inside Smart's network | Smart | Done | Foundation | Confirmed · email 21 Oct |
| Gateway software installed, registered and connected to Power BI | SBI, with server access and port openings from Smart | Not started | Foundation | Confirmed · email 21 Oct |
| Drivers: ClickHouse ODBC and the Impala or Hive driver, on developers' Desktop and on the gateway | SBI | Not started | Foundation | SBI · build spec |
| Service accounts for ClickHouse, PostgreSQL and Cloudera | Smart; SBI raises the requests | Not started | S1 build | Confirmed · email 21 Oct |
| Network path from the gateway to the three sources | Smart infra/network team | Unknown | Foundation | Unverified · notes 16 Sep |
| Fabric capacity provisioned in Smart's tenant; home region checked (Help → About → "Your data is stored in") | Smart | Unknown | Foundation | SBI · presales Q&A |
| SBI accounts, authoring licences and delegated admin roles in Smart's tenant; Smart keeps administrator rights | Smart | Unknown | Foundation | Unverified · notes 13 Oct |
| Version control platform – Azure DevOps, GitLab or SharePoint – and repository access | Smart decides | In progress | S1 build | Confirmed · email 21 Oct; Unverified · notes 16 Sep |
| Final inventory and P1/P2 order, frozen | Smart | Unknown | S1 build | SBI · proposal V2.0 |
| Inventory headers confirmed (pages vs visuals) | Smart | Unknown | S1 build | SBI · presales analysis |
| Acceptance criteria agreed, including objective visual criteria | SBI and Smart | In progress | S1 build | Confirmed · email 21 Oct |
| Baseline: average execution time per MicroStrategy report | Smart | Unknown | First UAT | Unverified · notes 16 Sep |
| MicroStrategy development freeze announced | Smart | Unknown | S1 build | Confirmed · workshop 3 Sep |

## Technical decisions log

The build is anchored on semantic models shared by fact tables, an AI-generated version 1, two release groups and per-report acceptance on data, visuals and response time. Most decisions below come from meeting notes – confirm the Unverified ones at kickoff.

| Date | Decision | Basis |
| --- | --- | --- |
| 22 Oct | Shared semantic models: dashboards with the same fact tables share one model when the consolidation conditions hold; one model per dashboard is the fallback | SBI · Greg, build spec |
| 22 Oct | A migration factory generates version 1 of all 151 dashboards in one run; the team refines them sprint by sprint in P1/P2 order | SBI · Greg |
| 13 Oct | Sprint dates are set at kickoff, separately from the agreement date | Unverified · notes 13 Oct |
| 13 Oct | SBI configures and integrates; Smart keeps Microsoft administrator rights and raises platform tickets with the distributor | Unverified · notes 13 Oct |
| 16 Sep | Acceptance per report on three aspects – data, visuals, response time no slower than today; no fixed % gain; measured manually in UAT, no stress test | Unverified · notes 16 Sep |
| 16 Sep | UAT is performed by Smart's IT team, not end users | Unverified · notes 16 Sep |
| 16 Sep | Workspace and permission design is defined at project start from today's access structure | Unverified · notes 16 Sep |
| 16 Sep | Refresh strategy is set per report, with incremental or table-level refresh where it helps | Unverified · notes 16 Sep |
| Early Sep | One semantic model per dashboard; cube count drives model effort, no consolidation ratio – superseded on 22 Oct | SBI · proposal V2.0 |
| Early Sep | Two release groups, P1 (98 dashboards) then P2 (53); the four heaviest dashboards open S1 | SBI · proposal V2.0 |
| 3 Sep | Development freeze on MicroStrategy for the whole migration | Confirmed · workshop 3 Sep |
| 3 Sep | Event-triggered refresh at the end of the ETL flow moves to after the migration | Confirmed · workshop 3 Sep |
| 28 Aug | SBI delivers every dashboard; Smart's team does not build | Unverified · notes 28 Aug |
| 16 Jul | Reporting layer only – warehouse and dimensional models unchanged | Unverified · discovery 16 Jul |

## Open technical questions and risks

The biggest open risk is MicroStrategy availability after 1 January 2027: the plan puts P1 go-live after that date, and every reconciliation compares against the live MicroStrategy report. Items are sorted by impact on the build.

| Open item | Why it matters | Owner |
| --- | --- | --- |
| MicroStrategy after 1 Jan 2027: procurement wrote that an extension is not an option (Confirmed · email), while notes of 3 Sep mention a one-year renewal (Unverified) | Parity checks and UAT need MicroStrategy running; if it stops, data and execution-time baselines must be exported per report beforehand | Greg, with Smart |
| Largest cube 21 GB / 141M rows (Unverified · notes 3 Sep) against a 25 GB model ceiling on F64; five cubes above 10 GB | Drives the SKU and the storage mode per model: import, DirectQuery, dual or aggregations. A dashboard whose own model is above 10 GB keeps a model of its own, flagged LARGE_MODEL (build spec, C5). Direct Lake is not a switch – it needs data in OneLake, outside today's scope | Tech Lead, measured in Foundation |
| Users: ~200, ~50 peak concurrent, ~40 authors (scoping answers) versus 10–15 active users (notes 3 Sep) | Workspace and audience design, concurrency load, number of authoring licences | Smart – pull counts from Platform Analytics |
| Inventory headers: is "Total Visualization Count" the page count? | Report effort is 0.5 h per visual; a wrong reading shifts every sprint | Smart |
| Rule that only aggregated data leaves on-premise (scoping answers) | Import models copy data into the Power BI service; confirm each cube in scope already meets the rule | Smart, confirmed in Foundation |
| Tenant and region: Smart saw a second (Malaysia) tenant as its "biggest roadblock" (notes 28 Aug); SBI recommends Fabric in the existing tenant | A second tenant means guest accounts, manual organisation switching and Conditional Access caveats | Smart M365 admin – home-region check |
| Admin roles: workshop of 3 Sep put capacity admin with SBI; notes of 13 Oct keep administrator rights with Smart | Defines the roles SBI needs during Foundation and what is handed back | Greg, with Smart |
| Access for SBI developers to MicroStrategy (Workstation, Library) to inspect dashboards and export data | Metadata is vendor-held, so the live dashboards are the only spec | Smart |
| Subscription list: reports, formats (PDF, Excel, email body), schedules, recipients | Distribution parity is in scope; today only "a few" are known | Smart |
| Version control provider: Azure DevOps, GitLab (used by Smart) or SharePoint; Smart asked on 21 Oct whether pipelines cover all 151 dashboards | Check that Fabric workspace Git integration supports the chosen provider before S1 | Smart decides, Tech Lead advises |
| Custom map: boundary shape files for Cambodian administrative divisions and the target visual (MapBox cited in scoping answers) | S1 deliverable; custom visuals may need tenant approval | Tech Lead, with Smart |
| Pre-filtered report copies: keep one-to-one or replace with row-level security | RLS sits with Smart unless moved into scope; consolidation would change the inventory | Smart |
| Refresh: ETL completion times per source | Time-based schedules must follow ETL, since event triggers come after migration | Smart |
| Sprint calendar: confirmed start date and the official 2026–27 Cambodian holidays (about 8 days assumed for Oct–Dec) | Capacity per sprint and the P1 date | SBI PM |

SQL and join patterns – GROUP BY, inner joins, composite keys, non-equi and role-playing joins – are raised per dashboard as decision flags by the migration factory and decided by the team; see the [build spec](docs/Migration_Factory_Build_Spec.md) (master copy: [artifact](https://claude.ai/artifact/W5bzpmyrDGtsmNJEh5ybR9)).

## Stakeholders and roles

Greg leads the technical design; Smart's analytics team (Samkhan, Khanheng) is the technical counterpart, and Smart's IT team signs off every report. Commercial and contract questions go to Jean-Luc, not to the delivery team.

| Person | Organisation | Role | Owns on this project |
| --- | --- | --- | --- |
| Greg Wolszczak | SBI | Technical lead and solution architect, ex-MicroStrategy | Architecture, Foundation design, technical decisions |
| Jean-Luc Khaou | SBI | Commercial lead | Contract, licensing, Microsoft distributor |
| Clément Pommier | SBI | Reviewed the cross-tenant SSO and Conditional Access answers in presales; on the project thread | Microsoft identity and security review |
| Panha Vuthy | SBI | Took the notes at the 3 Sep on-site workshop | Role to confirm |
| Cédric Kang | SBI | APAC management; invited to the 13 Oct review | Role to confirm |
| Tech Lead, 3 developers, PM | SBI | Delivery team – staffing in progress | Foundation, build, sprint management |
| Samkhan | Smart | Head of the Analytics Department | Senior counterpart for the reporting estate |
| Khanheng (Sok Khunheng) | Smart | Analytics Team Lead | Day-to-day technical counterpart |
| Smart IT team | Smart | UAT validators | Per-report sign-off |
| Smart infra, network and M365 admins | Smart | Infrastructure and tenant | Gateway server, ports, service accounts, tenant settings, administrator rights |
| So Chanpanha, Hany Abdalla | To confirm | Copied on the project thread | To confirm |
| Smart procurement and legal | Smart | Commercial and contract only | Handled by Jean-Luc |

**Escalation.** Microsoft platform issues: Smart raises the ticket with the distributor. Any security incident or data breach: tell Smart within 24 hours (both Unverified · notes 13 Oct).

## Sources

Ten sources feed this doc. Not reached: the signed SOW, any proposal version after 16 Sep and the email attachments (Q&A answers, draft acceptance criteria) – SharePoint and Outlook were seen only through Notion search excerpts.

| Source | Date | Holds | Basis |
| --- | --- | --- | --- |
| [Outlook thread "Smart / SBI - Support requirement"](https://outlook.office365.com/owa/?ItemID=AAMkAGU1OWE1ODM1LWIwOTAtNGNjZS1iZWNlLWE2MzgxOWIwZWE0ZABGAAAAAABOOceJoWLXR5XqBEP3xyxJBwBClBbL0teKRKu%2B%2Buz%2BSPUkAAAAAAEMAABClBbL0teKRKu%2B%2Buz%2BSPUkAAXkQgPSAAA%3D&exvsurl=1&viewmodel=ReadMessageItem) | 21 Oct | Readiness: gateway, service accounts, acceptance criteria, Git question | Confirmed (excerpt only) |
| [Review meeting notes](https://app.notion.com/p/3f8b1344ecea8147b1cece21b3512c35) | 13 Oct | Sprint dates at kickoff, admin rights, escalation – technical points only | Unverified |
| [Proposal clarification meeting](https://app.notion.com/p/3ddb1344ecea80189ac4ec61890ecc56) | 16 Sep | Acceptance aspects, UAT flow, sprints, gateway, refresh, deliverables | Unverified |
| [Presales chat](https://claude.ai/chat/31b20a5f-725d-4347-b3f1-ea4eb611fbfb) | Jul–Oct | Proposal V2.0, estimation, Greg's on-site confirmations, Microsoft Q&A | SBI; on-site points Confirmed |
| Client inventory MSTR_Dashboards_to_be_migrated.xlsx (uploaded in the presales chat) | Sep | 151 dashboards with pages, visuals, cubes, P1/P2 | Confirmed |
| [Workshop notes PDF](https://solutionbi-my.sharepoint.com/personal/panha_vuthy_sbi-group_com/Documents/Microsoft%20Teams%20Chat%20Files/Smart_Axiata%20-%20MSTR__Power_BI_Fabric_Migration%20-%20Meeting.pdf) | 3 Sep | Earlier counts, largest cube, user numbers | Unverified |
| [Support requirement meeting](https://app.notion.com/p/3cab1344ecea812092e3e294677888f5) | 28 Aug | Scope cut to 150, MTDI share, tenant concern, security and Git topics | Unverified |
| [Teams DM Greg / Jean-Luc](https://teams.microsoft.com/l/message/19:5444685d-86eb-4405-95be-bb06918d4c9a_fef0fbcc-b9a9-4e57-8ddc-184d73dacec1@unq.gbl.spaces/1787050484286?context=%7B%22contextType%22%3A%22chat%22%7D) | 18 Aug | Phase 0 and on-premises gateway explanation; proposal drafts on SharePoint | SBI |
| [Discovery call notes](https://app.notion.com/p/39fb1344ecea80168f92ee6c02452351) | 16 Jul | Drivers, Cloudera CDP, managed MicroStrategy cloud, M365 without Azure | Unverified |
| APAC Weekly Staffing (Notion) | Oct | Staffing status only; not linked, holds candidate details | SBI |

Reference for kickoff prep: SBI's [kickoff deck for a previous Fabric project in Cambodia](https://solutionbi.sharepoint.com/sites/SBI/OPE/CIA_KHM_Data-Platform/Delivery/8.%20KickOff%20Prez/CambodiaAirportxSBI_KickOff_250625.pdf) (Jun 2025) lists tenant, licence and admin-account prerequisites in a reusable format.
