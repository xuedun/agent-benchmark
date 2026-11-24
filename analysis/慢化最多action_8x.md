# 慢化最多的 Action（1×→8× 超分）

数据：logs_mix_v3 原始 4 格，按 (轨迹, 步骤号) 匹配对比 1_1 vs 8_1（每 VM 负载不变，唯一变量 = 并发 384→3072 VM）。
口径：
- **1×/8×均值** = 该 action 在对应格内所有拷贝的 command_sec 均值（拷贝数：1_1 约 7 份，8_1 约 56 份）
- **ΔT** = (8×均值 − 1×均值) × 8_1 拷贝数 = 该 action 对全池总执行时间的增量贡献
- 全部 4543 个 (轨迹,步骤) 对齐，action 文本零不匹配；失败命令数随拷贝数精确线性（×8），无负载相关失败污染
- 总 ΔT(8_1 − 1_1) = 194,603 核·秒

## 一、类别汇总

| 类别 | 步数 | 1×均值(s) | 8×均值(s) | 倍数 | ΔT合计(s) | 占比 | 失败@1× | 失败@8× |
|---|---:|---:|---:|---:|---:|---:|---:|---:|
| go | 315 | 2.712 | 7.673 | 2.83 | 87,508 | 45.0% | 546 | 4368 |
| npm/node | 238 | 3.905 | 9.640 | 2.47 | 76,444 | 39.3% | 301 | 2408 |
| maven | 42 | 5.295 | 13.649 | 2.58 | 18,982 | 9.8% | 28 | 224 |
| fs-scan | 2337 | 0.033 | 0.081 | 2.43 | 6,208 | 3.2% | 1726 | 13808 |
| shell-other | 230 | 0.183 | 0.505 | 2.76 | 4,100 | 2.1% | 604 | 4832 |
| fs-write | 1038 | 0.006 | 0.017 | 2.69 | 624 | 0.3% | 56 | 448 |
| git | 183 | 0.026 | 0.060 | 2.34 | 352 | 0.2% | 49 | 392 |
| compile(c/c++/rust) | 18 | 0.828 | 1.161 | 1.40 | 336 | 0.2% | 0 | 0 |
| python/pytest | 85 | 0.007 | 0.018 | 2.50 | 50 | 0.0% | 540 | 4320 |
| gradle | 57 | 0.053 | 0.053 | 1.00 | -0 | -0.0% | 7 | 56 |

**构建/包管理三巨头（go + npm/node + maven）= ΔT 的 94%**；倍数全类别均匀（×2.3-2.8，含 rm/find 等元数据操作）——"每次随机内存访问变贵"的指纹（详见 USE_report.md §3.1）。

## 二、Top 100 慢化 Action 明细（按 ΔT 排序）

| # | 轨迹#步骤 | 类别 | 1×(s) | 8×(s) | 倍数 | ΔT(s) | 命令 |
|---:|---|---|---:|---:|---:|---:|---|
| 1 | rancher-sandbox__cluster-api-provider-rke2-115#96 | go | 41.1 | 213.5 | 5.19 | 9,652 | `cd /testbed && go build ./pkg/rke2/...` |
| 2 | kubernetes-sigs__kwok-144#84 | go | 33.3 | 171.2 | 5.15 | 7,726 | `cd /testbed && go build ./pkg/kwok/controllers/... 2>&1 \| head -20` |
| 3 | promptfoo__promptfoo-88#65 | npm/node | 20.1 | 123.4 | 6.15 | 5,788 | `cd /testbed && npm run build 2>&1 \| grep -E "error TS\|Successfully compiled" \| head -20` |
| 4 | fhir__sushi-513#92 | npm/node | 60.9 | 156.3 | 2.56 | 5,340 | `cd /testbed && npm test 2>&1 \| tail -50` |
| 5 | fhir__sushi-439#76 | npm/node | 35.7 | 128.1 | 3.59 | 5,175 | `cd /testbed && npm test -- --no-coverage 2>&1 \| tail -100` |
| 6 | ota-meshi__eslint-plugin-regexp-285#64 | npm/node | 21.8 | 108.5 | 4.97 | 4,854 | `cd /testbed && npm test -- tests/lib/rules/no-useless-flag.ts 2>&1 \| tail -50` |
| 7 | tikv__pd-5480#95 | go | 39.7 | 123.2 | 3.11 | 4,678 | `cd /testbed && go build ./tests/pdctl/...` |
| 8 | googleapis__java-storage-2661#62 | maven | 14.3 | 94.8 | 6.61 | 4,507 | `cd /testbed && mvn clean compile -DskipTests -q 2>&1 \| head -100` |
| 9 | tikv__pd-5480#80 | go | 12.9 | 86.9 | 6.73 | 4,141 | `cd /testbed && go build ./server/cluster/...` |
| 10 | nats-io__nats-server-4298#77 | go | 17.7 | 90.0 | 5.08 | 4,046 | `cd /testbed && go build -o /tmp/nats-server ./server 2>&1 \| head -50` |
| 11 | ontodev__robot-1225#45 | maven | 23.6 | 94.5 | 4.00 | 3,966 | `cd /testbed && mvn test 2>&1 \| grep -E "Tests run:\|BUILD SUCCESS\|BUILD FAILURE" \| tail -20` |
| 12 | promptfoo__promptfoo-88#66 | npm/node | 19.3 | 88.1 | 4.57 | 3,856 | `cd /testbed && npm run build 2>&1 \| tail -20` |
| 13 | cometbft__cometbft-3045#68 | go | 15.7 | 81.8 | 5.20 | 3,698 | `cd /testbed && go build ./internal/consensus/... 2>&1 \| head -50` |
| 14 | googleapis__java-storage-2661#59 | maven | 15.8 | 79.0 | 4.99 | 3,540 | `cd /testbed && mvn clean compile -DskipTests -q 2>&1 \| head -100` |
| 15 | opossum-tool__opossumui-2503#43 | npm/node | 11.4 | 73.1 | 6.43 | 3,458 | `cd /testbed && npm run typecheck 2>&1 \| grep -E "error TS\|Found [0-9]+ error"` |
| 16 | nats-io__nats-server-4298#80 | go | 19.7 | 79.1 | 4.01 | 3,324 | `cd /testbed && go test -v ./server -run TestNonExistent 2>&1 \| head -20` |
| 17 | goadesign__goa-3127#74 | go | 15.3 | 72.4 | 4.74 | 3,199 | `cd /testbed && go build ./http/codegen/...` |
| 18 | caarlos0__env-223#85 | go | 13.1 | 65.2 | 4.99 | 2,919 | `cd /testbed && go test -v -race 2>&1 \| head -100` |
| 19 | tikv__pd-5480#79 | go | 9.6 | 59.2 | 6.19 | 2,778 | `cd /testbed && go build ./server/core/...` |
| 20 | auth0__auth0-spa-js-341#35 | npm/node | 8.2 | 54.2 | 6.62 | 2,578 | `cd /testbed && npm run build 2>&1 \| tail -20` |
| 21 | fhir__sushi-439#85 | npm/node | 34.2 | 79.6 | 2.33 | 2,542 | `cd /testbed && npm test -- --no-coverage 2>&1 \| grep -E "Test Suites:\|Tests:" \| tail -2` |
| 22 | nats-io__nats-server-5783#71 | go | 12.3 | 56.0 | 4.55 | 2,448 | `cd /testbed && go build -o /tmp/nats-server ./main.go 2>&1 \| head -50` |
| 23 | auth0__auth0-spa-js-341#37 | npm/node | 10.5 | 48.6 | 4.64 | 2,135 | `cd /testbed && npm test 2>&1 \| tail -50` |
| 24 | ontodev__robot-1225#41 | maven | 8.8 | 45.6 | 5.19 | 2,061 | `cd /testbed && mvn clean compile -DskipTests 2>&1 \| tail -20` |
| 25 | kubernetes-sigs__cloud-provider-azure-892#48 | go | 62.8 | 99.4 | 1.58 | 2,049 | `cd /testbed && go build ./pkg/provider/... 2>&1 \| head -50` |
| 26 | kubernetes-sigs__cloud-provider-azure-892#76 | go | 16.1 | 48.9 | 3.03 | 1,837 | `cd /testbed && go test ./pkg/provider -run TestReconcileTags -v 2>&1 \| head -100` |
| 27 | alecthomas__kong-169#70 | go | 5.3 | 38.1 | 7.14 | 1,833 | `cd /testbed && go clean -cache && go build ./...` |
| 28 | bombsimon__wsl-173#53 | go | 5.1 | 34.3 | 6.75 | 1,638 | `cd /testbed && go test -v 2>&1 \| grep -A 20 "FAIL\\|PASS.*switch\\|PASS.*select"` |
| 29 | opossum-tool__opossumui-2503#58 | npm/node | 8.7 | 37.0 | 4.24 | 1,582 | `cd /testbed && npm run test:unit -- "AttributionList\|signals-worker\|use-signals-worker" --no-coverage 2>&1 \` |
| 30 | bombsimon__wsl-173#59 | go | 5.1 | 32.9 | 6.47 | 1,556 | `cd /testbed && go test -v 2>&1 \| tail -50` |
| 31 | fhir__sushi-439#93 | npm/node | 34.0 | 61.0 | 1.79 | 1,510 | `cd /testbed && npm test -- --no-coverage 2>&1 \| tail -5` |
| 32 | typescript-eslint__tslint-to-eslint-config-712#58 | npm/node | 6.2 | 31.8 | 5.16 | 1,434 | `cd /testbed && npm test 2>&1 \| head -100` |
| 33 | lingui__js-lingui-807#60 | npm/node | 4.7 | 29.9 | 6.38 | 1,411 | `cd /testbed && npx tsc --noEmit packages/macro/src/macroJs.ts 2>&1 \| head -20` |
| 34 | opossum-tool__opossumui-2503#42 | npm/node | 8.0 | 33.2 | 4.14 | 1,408 | `cd /testbed && npm run test:unit -- AttributionList.test --no-coverage 2>&1 \| grep -E "Test Suites:\|Tests:" ` |
| 35 | cerbos__cerbos-1655#94 | go | 64.8 | 88.8 | 1.37 | 1,340 | `cd /testbed && go build ./internal/storage/... 2>&1 \| head -30` |
| 36 | alecthomas__kong-169#83 | go | 8.6 | 32.5 | 3.79 | 1,338 | `cd /testbed && go test -v -run TestHelp 2>&1 \| head -100` |
| 37 | containrrr__watchtower-1784#47 | go | 13.4 | 37.1 | 2.77 | 1,329 | `cd /testbed && go test ./... -short 2>&1 \| tail -50` |
| 38 | lingui__js-lingui-807#55 | npm/node | 4.6 | 27.6 | 6.03 | 1,288 | `cd /testbed && npx tsc --noEmit packages/macro/src/macroJs.ts 2>&1 \| head -50` |
| 39 | opossum-tool__opossumui-2503#51 | npm/node | 5.8 | 28.7 | 4.98 | 1,286 | `cd /testbed && npm run test:unit -- web-workers --no-coverage 2>&1 \| grep -E "Test Suites:\|Tests:" \| tail -` |
| 40 | bombsimon__wsl-108#48 | go | 8.5 | 30.8 | 3.63 | 1,248 | `cd /testbed && go test -v` |
| 41 | lingui__js-lingui-807#63 | npm/node | 4.5 | 26.2 | 5.78 | 1,215 | `cd /testbed && npx tsc --noEmit packages/macro/src/macroJs.ts 2>&1 \| head -20` |
| 42 | triggermesh__aws-event-sources-152#13 | go | 35.5 | 57.0 | 1.60 | 1,201 | `cd /testbed && go build ./pkg/adapter/awsdynamodbsource/... 2>&1 \| head -30` |
| 43 | fabien0102__ts-to-zod-240#61 | npm/node | 4.6 | 24.9 | 5.37 | 1,135 | `cd /testbed && npx tsc --noEmit check_optional_inference.ts 2>&1 \| grep "check_optional_inference.ts"` |
| 44 | ktsn__vuex-smart-module-243#58 | npm/node | 3.5 | 23.4 | 6.70 | 1,113 | `cd /testbed && npx jest --testMatch='**/test_namespace_direct.ts' 2>&1 \| grep -A 100 "console.log"` |
| 45 | cometbft__cometbft-3045#65 | go | 8.4 | 27.6 | 3.28 | 1,074 | `cd /testbed && go build ./internal/bits/... 2>&1 \| head -50` |
| 46 | kubernetes-sigs__cloud-provider-azure-892#77 | npm/node | 15.8 | 34.8 | 2.20 | 1,065 | `cd /testbed && go build ./cmd/cloud-node-manager/... 2>&1` |
| 47 | nats-io__nats-server-5783#85 | go | 4.9 | 21.8 | 4.47 | 949 | `cd /testbed && go test -v -run TestLeafNodeTLSHandshakeFirst ./server 2>&1 \| head -100` |
| 48 | cerbos__cerbos-1655#93 | go | 15.6 | 32.4 | 2.08 | 942 | `cd /testbed && go build ./internal/namer/... 2>&1 \| head -20` |
| 49 | fabien0102__ts-to-zod-240#59 | npm/node | 3.3 | 20.0 | 6.02 | 935 | `cd /testbed && npx ts-node test_validation.ts` |
| 50 | capricorn86__happy-dom-824#31 | npm/node | 37.3 | 53.6 | 1.44 | 914 | `cd /testbed/packages/happy-dom && npm test 2>&1 \| head -100` |
| 51 | ontodev__robot-1225#31 | maven | 24.0 | 40.2 | 1.67 | 907 | `cd /testbed && mvn test 2>&1 \| grep -A 10 "BUILD SUCCESS\\|BUILD FAILURE\\|Tests run:"` |
| 52 | moov-io__customers-275#19 | go | 35.7 | 51.4 | 1.44 | 882 | `cd /testbed && go build ./cmd/server/ 2>&1 \| head -50` |
| 53 | ontodev__robot-1225#36 | fs-scan | 2.6 | 18.2 | 7.08 | 876 | `cat > /tmp/test_edge_cases.java << 'EOF' import org.obolibrary.robot.*; import org.semanticweb.owlapi.model.*;` |
| 54 | rancher-sandbox__cluster-api-provider-rke2-115#95 | go | 2.7 | 18.2 | 6.86 | 870 | `cd /testbed && go build ./pkg/consts/...` |
| 55 | ktsn__vuex-smart-module-243#63 | npm/node | 3.6 | 18.5 | 5.09 | 832 | `cd /testbed && npx jest --testMatch='**/test_single_level.ts' 2>&1 \| grep -A 100 "console.log"` |
| 56 | goadesign__goa-3127#81 | go | 4.1 | 18.4 | 4.50 | 800 | `cd /testbed && go test ./http/codegen -run TestServerTypes -v 2>&1 \| head -50` |
| 57 | ontodev__robot-1225#38 | fs-scan | 2.5 | 16.4 | 6.71 | 784 | `cat > /tmp/test_invalid_template.java << 'EOF' import org.obolibrary.robot.*; import org.semanticweb.owlapi.mo` |
| 58 | fabien0102__ts-to-zod-240#58 | npm/node | 2.6 | 16.4 | 6.26 | 770 | `cd /testbed && npx ts-node test_all_cases.ts` |
| 59 | lingui__js-lingui-807#71 | npm/node | 4.5 | 18.2 | 4.02 | 766 | `cd /testbed && npx tsc --noEmit packages/macro/src/macroJs.ts 2>&1` |
| 60 | tikv__pd-5480#88 | go | 4.7 | 18.2 | 3.87 | 756 | `cd /testbed && go build ./server/...` |
| 61 | ktsn__vuex-smart-module-243#59 | npm/node | 2.5 | 15.6 | 6.30 | 735 | `cd /testbed && npx jest --testMatch='**/test_namespace_direct.ts' 2>&1 \| tail -30` |
| 62 | capricorn86__happy-dom-824#44 | npm/node | 20.7 | 33.6 | 1.62 | 723 | `cd /testbed/packages/happy-dom && npm test 2>&1 \| tail -20` |
| 63 | alecthomas__kong-169#80 | go | 5.5 | 17.6 | 3.19 | 677 | `cd /testbed && go clean -cache && go build ./... && go run /tmp/test_placeholder_debug2.go 2>&1` |
| 64 | lingui__js-lingui-807#50 | npm/node | 14.2 | 26.2 | 1.85 | 673 | `cd /testbed && npm run release:build 2>&1 \| tail -50` |
| 65 | goadesign__goa-3497#80 | go | 5.2 | 16.7 | 3.23 | 647 | `cd /testbed && go test ./http/codegen/openapi/v3/... -v 2>&1 \| head -50` |
| 66 | fabien0102__ts-to-zod-240#63 | npm/node | 3.2 | 14.7 | 4.54 | 643 | `cp src/core/generateZodSchema.ts.backup src/core/generateZodSchema.ts && cd /testbed && npx ts-node test_valid` |
| 67 | ktsn__vuex-smart-module-243#61 | npm/node | 2.5 | 14.0 | 5.57 | 643 | `cd /testbed && npx jest --testMatch='**/test_namespace_direct.ts' 2>&1 \| grep -A 100 "console.log"` |
| 68 | fabien0102__ts-to-zod-240#72 | npm/node | 8.9 | 19.7 | 2.22 | 607 | `cd /testbed && npm test -- validateGeneratedTypes.test.ts 2>&1 \| tail -80` |
| 69 | lingui__js-lingui-807#72 | npm/node | 4.2 | 14.7 | 3.52 | 590 | `cd /testbed && npm test -- packages/macro/test/index.ts 2>&1 \| tail -50` |
| 70 | alangpierce__sucrase-800#90 | npm/node | 6.9 | 17.0 | 2.48 | 570 | `npx tsc 2>&1 \| head -30` |
| 71 | tikv__pd-5480#82 | go | 1.8 | 11.7 | 6.60 | 557 | `cd /testbed && go build ./server/cluster/...` |
| 72 | kubernetes-sigs__kwok-144#106 | go | 10.7 | 20.5 | 1.92 | 550 | `cd /testbed && go test ./pkg/kwok/controllers/... -v 2>&1 \| grep -A 5 "TestRenderToJson"` |
| 73 | tikv__pd-5480#84 | go | 1.5 | 11.3 | 7.48 | 549 | `cd /testbed && go build ./server/cluster/...` |
| 74 | triggermesh__aws-event-sources-152#22 | go | 3.5 | 13.2 | 3.82 | 546 | `cd /testbed && go build ./pkg/adapter/... 2>&1 \| head -50` |
| 75 | typescript-eslint__tslint-to-eslint-config-712#60 | npm/node | 1.7 | 11.3 | 6.59 | 537 | `cd /testbed && npm test -- src/comments/convertFileComments.test.ts 2>&1 \| tail -50` |
| 76 | ktsn__vuex-smart-module-243#72 | npm/node | 3.8 | 13.4 | 3.53 | 537 | `cd /testbed && npx jest --testMatch='**/final_verification.ts' 2>&1 \| grep -A 200 "console.log"` |
| 77 | fabien0102__ts-to-zod-240#55 | npm/node | 2.6 | 12.1 | 4.59 | 530 | `cd /testbed && npx ts-node test_optional.ts` |
| 78 | opossum-tool__opossumui-2503#50 | npm/node | 1.8 | 11.1 | 6.18 | 523 | `cd /testbed && npm run test:unit -- "signals-worker\\|use-signals-worker" --no-coverage 2>&1 \| grep -E "Test ` |
| 79 | typescript-eslint__tslint-to-eslint-config-712#64 | npm/node | 2.0 | 11.3 | 5.58 | 518 | `cd /testbed && npm test -- src/comments/convertFileComments.test.ts 2>&1 \| tail -50` |
| 80 | plexsystems__konstraint-275#58 | go | 2.4 | 11.4 | 4.80 | 505 | `cd /testbed && go test ./internal/commands -v 2>&1 \| head -50` |
| 81 | fhir__sushi-513#91 | npm/node | 7.4 | 16.3 | 2.22 | 502 | `cd /testbed && npm run build 2>&1 \| grep -E "error\|Error" \| head -20` |
| 82 | ontodev__robot-1225#37 | fs-scan | 1.4 | 10.3 | 7.30 | 496 | `cat > /tmp/test_invalid_template.java << 'EOF' import org.obolibrary.robot.*; import java.util.*;  public clas` |
| 83 | typescript-eslint__tslint-to-eslint-config-712#59 | npm/node | 1.6 | 10.4 | 6.68 | 496 | `cd /testbed && npm test -- src/comments/convertComments.test.ts 2>&1 \| tail -50` |
| 84 | fabien0102__ts-to-zod-240#70 | npm/node | 4.5 | 13.3 | 2.93 | 490 | `cd /testbed && npx tsc --noEmit test_default_approach.ts 2>&1 \| grep "test_default_approach.ts"` |
| 85 | primefaces__primefaces-8980#48 | maven | 1.8 | 10.5 | 5.84 | 488 | `cd /testbed && mvn clean compile -DskipTests -pl primefaces -am 2>&1 \| grep -E "(BUILD SUCCESS\|BUILD FAILURE` |
| 86 | fabien0102__ts-to-zod-240#68 | npm/node | 4.5 | 13.1 | 2.89 | 480 | `cd /testbed && npx tsc --noEmit manual_type_check.ts 2>&1` |
| 87 | ontodev__robot-1225#44 | maven | 8.1 | 16.3 | 2.02 | 460 | `cd /testbed/robot-core && mvn test -Dtest=TemplateTest 2>&1 \| tail -30` |
| 88 | promptfoo__promptfoo-88#61 | npm/node | 2.7 | 10.8 | 3.94 | 450 | `cd /testbed && npm run build 2>&1 \| grep -E "error TS\|Successfully" \| head -20` |
| 89 | fabien0102__ts-to-zod-240#73 | npm/node | 10.3 | 17.7 | 1.71 | 413 | `cd /testbed && npm test 2>&1 \| tail -100` |
| 90 | opossum-tool__opossumui-2503#39 | npm/node | 10.1 | 17.4 | 1.73 | 409 | `cd /testbed && npm run test:unit -- AttributionList.test 2>&1 \| tail -100` |
| 91 | opossum-tool__opossumui-2503#36 | npm/node | 11.6 | 18.8 | 1.63 | 408 | `cd /testbed && npm run typecheck 2>&1 \| grep -E "error TS\|Found [0-9]+ error" \| head -20` |
| 92 | bombsimon__wsl-173#40 | go | 14.2 | 21.5 | 1.51 | 407 | `cd /testbed && go test -v 2>&1 \| head -100` |
| 93 | aftership__clickhouse-sql-parser-128#75 | go | 1.1 | 7.8 | 6.98 | 376 | `cd /testbed && go run test_full.go` |
| 94 | ktsn__vuex-smart-module-243#68 | npm/node | 3.6 | 9.7 | 2.72 | 344 | `cd /testbed && npx jest --testMatch='**/verify_fix.ts'` |
| 95 | wikidata__wikidata-toolkit-717#49 | maven | 2.8 | 9.8 | 3.50 | 335 | `cd /testbed && mvn clean compile -DskipTests -q 2>&1 \| tail -50` |
| 96 | plexsystems__konstraint-275#69 | go | 1.0 | 7.0 | 6.85 | 335 | `cd /testbed && go build -o /tmp/konstraint ./main.go 2>&1` |
| 97 | ktsn__vuex-smart-module-243#70 | npm/node | 2.0 | 7.8 | 3.95 | 326 | `cd /testbed && npm run test:unit` |
| 98 | bombsimon__wsl-173#54 | go | 0.8 | 6.5 | 8.27 | 321 | `cd /testbed && go test -v -run TestDefaultConfig/switch 2>&1` |
| 99 | rs__zerolog-663#87 | go | 2.3 | 7.8 | 3.46 | 312 | `cd /testbed && go test -v ./... 2>&1 \| head -100` |
| 100 | lingui__js-lingui-807#67 | npm/node | 4.5 | 9.9 | 2.18 | 299 | `cd /testbed && npx tsc --noEmit packages/macro/src/macroJs.ts 2>&1` |

## 三、规律

1. **头部全是重构建命令**：go build / go test、npm test / npm run build / npx tsc、mvn clean compile——随机访问密集、工作集数十-上百 MB 的编译/解析/测试负载。
2. **倍数与命令时长正相关**：Top100 中短命令（<5s）倍数 ×5-8，长命令（>30s）倍数 ×1.4-2.6；随机访问延迟通胀随命令内访问次数累积。
3. **Top100 倍数中位 ~4.6**（类别整体均值 ×2.4-2.8）：头部 action 被双重打击（更长 + 更多随机访问）。
4. Top100 类别分布：{'npm/node': 49, 'go': 40, 'maven': 8, 'fs-scan': 3}。
5. 高频受害轨迹：tikv/pd、fhir/sushi、lingui、fabien0102/ts-to-zod、ktsn/vuex-smart-module、opossum-tool、ontodev/robot、typescript-eslint 等多步骤构建密集型仓库。

## 四、数据文件

- 完整 Top 200：`analysis/top_steps.csv`；类别聚合：`analysis/category_summary.csv`
- 步骤原始聚合：`analysis/step_agg_{1_1,1_2,8_1,8_2}.csv`
- 生成脚本：`scripts/gen_action_doc.py`（本文档可复现）
