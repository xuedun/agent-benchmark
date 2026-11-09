#!/bin/bash
# 根据本机架构，只拉取 55 条轨迹对应的 ARM64 或 x86_64 镜像。
# 清单与 artifacts/traces/replays/、artifacts/traces/image-map.json 对应。
set -euo pipefail

machine_arch="$(uname -m)"
case "$machine_arch" in
    aarch64|arm64)
        image_arch=arm64
        ;;
    x86_64|amd64)
        image_arch=x86
        ;;
    *)
        echo "Unsupported machine architecture: $machine_arch" >&2
        exit 1
        ;;
esac

echo "Pulling 55 images for $image_arch ($machine_arch)"
case "$image_arch" in
    arm64)
        docker pull --platform linux/arm64 swr.cn-north-4.myhuaweicloud.com/kunpeng-ai/swerebench-arm64-aftership-clickhouse-sql-parser:128-8bea76f
        docker pull --platform linux/arm64 swr.cn-north-4.myhuaweicloud.com/kunpeng-ai/swerebench-arm64-alangpierce-sucrase:800-328cc66
        docker pull --platform linux/arm64 swr.cn-north-4.myhuaweicloud.com/kunpeng-ai/swerebench-arm64-alecthomas-kong:169-9c81441
        docker pull --platform linux/arm64 swr.cn-north-4.myhuaweicloud.com/kunpeng-ai/swerebench-arm64-apigee-registry:1142-6e76c8a
        docker pull --platform linux/arm64 swr.cn-north-4.myhuaweicloud.com/kunpeng-ai/swerebench-arm64-aquasecurity-tfsec:969-8808315
        docker pull --platform linux/arm64 swr.cn-north-4.myhuaweicloud.com/kunpeng-ai/swerebench-arm64-auth0-auth0-spa-js:341-6192758
        docker pull --platform linux/arm64 swr.cn-north-4.myhuaweicloud.com/kunpeng-ai/swerebench-arm64-bombsimon-wsl:108-bbddf87
        docker pull --platform linux/arm64 swr.cn-north-4.myhuaweicloud.com/kunpeng-ai/swerebench-arm64-bombsimon-wsl:173-9bcfb87
        docker pull --platform linux/arm64 swr.cn-north-4.myhuaweicloud.com/kunpeng-ai/swerebench-arm64-caarlos0-env:223-863e59d
        docker pull --platform linux/arm64 swr.cn-north-4.myhuaweicloud.com/kunpeng-ai/swerebench-arm64-capricorn86-happy-dom:824-fdbfc53
        docker pull --platform linux/arm64 swr.cn-north-4.myhuaweicloud.com/kunpeng-ai/swerebench-arm64-ceph-go-ceph:889-4787861
        docker pull --platform linux/arm64 swr.cn-north-4.myhuaweicloud.com/kunpeng-ai/swerebench-arm64-cerbos-cerbos:1655-a7d5e37
        docker pull --platform linux/arm64 swr.cn-north-4.myhuaweicloud.com/kunpeng-ai/swerebench-arm64-cometbft-cometbft:3045-fec9682
        docker pull --platform linux/arm64 swr.cn-north-4.myhuaweicloud.com/kunpeng-ai/swerebench-arm64-containrrr-watchtower:1784-9180e95
        docker pull --platform linux/arm64 swr.cn-north-4.myhuaweicloud.com/kunpeng-ai/swerebench-arm64-datreeio-datree:401-799d634
        docker pull --platform linux/arm64 swr.cn-north-4.myhuaweicloud.com/kunpeng-ai/swerebench-arm64-editorconfig-checker-editorconfig-checker:126-01235e1
        docker pull --platform linux/arm64 swr.cn-north-4.myhuaweicloud.com/kunpeng-ai/swerebench-arm64-fabien0102-ts-to-zod:240-16b5ce6
        docker pull --platform linux/arm64 swr.cn-north-4.myhuaweicloud.com/kunpeng-ai/swerebench-arm64-fhir-sushi:439-3cf43a3
        docker pull --platform linux/arm64 swr.cn-north-4.myhuaweicloud.com/kunpeng-ai/swerebench-arm64-fhir-sushi:513-709158e
        docker pull --platform linux/arm64 swr.cn-north-4.myhuaweicloud.com/kunpeng-ai/swerebench-arm64-formidablelabs-urql:410-f7e207b
        docker pull --platform linux/arm64 swr.cn-north-4.myhuaweicloud.com/kunpeng-ai/swerebench-arm64-gavv-httpexpect:230-ad7be84
        docker pull --platform linux/arm64 swr.cn-north-4.myhuaweicloud.com/kunpeng-ai/swerebench-arm64-goadesign-goa:3127-5af5c8a
        docker pull --platform linux/arm64 swr.cn-north-4.myhuaweicloud.com/kunpeng-ai/swerebench-arm64-goadesign-goa:3497-e41215d
        docker pull --platform linux/arm64 swr.cn-north-4.myhuaweicloud.com/kunpeng-ai/swerebench-arm64-googleapis-java-storage:2661-53fbba7
        docker pull --platform linux/arm64 swr.cn-north-4.myhuaweicloud.com/kunpeng-ai/swerebench-arm64-hetznercloud-hcloud-go:45-482fc40
        docker pull --platform linux/arm64 swr.cn-north-4.myhuaweicloud.com/kunpeng-ai/swerebench-arm64-hipparchus-math-hipparchus:266-1681d1b
        docker pull --platform linux/arm64 swr.cn-north-4.myhuaweicloud.com/kunpeng-ai/swerebench-arm64-jhipster-generator-jhipster:9405-637aafb
        docker pull --platform linux/arm64 swr.cn-north-4.myhuaweicloud.com/kunpeng-ai/swerebench-arm64-ktsn-vuex-smart-module:243-4edbc13
        docker pull --platform linux/arm64 swr.cn-north-4.myhuaweicloud.com/kunpeng-ai/swerebench-arm64-kubernetes-sigs-cloud-provider-azure:892-46f2c54
        docker pull --platform linux/arm64 swr.cn-north-4.myhuaweicloud.com/kunpeng-ai/swerebench-arm64-kubernetes-sigs-kwok:144-8c0fd3e
        docker pull --platform linux/arm64 swr.cn-north-4.myhuaweicloud.com/kunpeng-ai/swerebench-arm64-lingui-js-lingui:807-cbe946e
        docker pull --platform linux/arm64 swr.cn-north-4.myhuaweicloud.com/kunpeng-ai/swerebench-arm64-microsoft-mssql-jdbc:1838-7ffc2f0
        docker pull --platform linux/arm64 swr.cn-north-4.myhuaweicloud.com/kunpeng-ai/swerebench-arm64-mmcdole-gofeed:259-6e8dddc
        docker pull --platform linux/arm64 swr.cn-north-4.myhuaweicloud.com/kunpeng-ai/swerebench-arm64-moov-io-customers:275-fce1954
        docker pull --platform linux/arm64 swr.cn-north-4.myhuaweicloud.com/kunpeng-ai/swerebench-arm64-nats-io-nats-server:4298-37d3220
        docker pull --platform linux/arm64 swr.cn-north-4.myhuaweicloud.com/kunpeng-ai/swerebench-arm64-nats-io-nats-server:5783-1e1169b
        docker pull --platform linux/arm64 swr.cn-north-4.myhuaweicloud.com/kunpeng-ai/swerebench-arm64-ontodev-robot:1225-27d5fd3
        docker pull --platform linux/arm64 swr.cn-north-4.myhuaweicloud.com/kunpeng-ai/swerebench-arm64-openrefine-openrefine:6161-307d8bf
        docker pull --platform linux/arm64 swr.cn-north-4.myhuaweicloud.com/kunpeng-ai/swerebench-arm64-openrefine-openrefine:7176-d92d8e5
        docker pull --platform linux/arm64 swr.cn-north-4.myhuaweicloud.com/kunpeng-ai/swerebench-arm64-opossum-tool-opossumui:2503-0da6d46
        docker pull --platform linux/arm64 swr.cn-north-4.myhuaweicloud.com/kunpeng-ai/swerebench-arm64-ota-meshi-eslint-plugin-regexp:285-0eced9d
        docker pull --platform linux/arm64 swr.cn-north-4.myhuaweicloud.com/kunpeng-ai/swerebench-arm64-ota-meshi-eslint-plugin-svelte:302-1349cf7
        docker pull --platform linux/arm64 swr.cn-north-4.myhuaweicloud.com/kunpeng-ai/swerebench-arm64-plexsystems-konstraint:275-2113156
        docker pull --platform linux/arm64 swr.cn-north-4.myhuaweicloud.com/kunpeng-ai/swerebench-arm64-primefaces-primefaces:8980-d09ef48
        docker pull --platform linux/arm64 swr.cn-north-4.myhuaweicloud.com/kunpeng-ai/swerebench-arm64-promptfoo-promptfoo:88-8de6e12
        docker pull --platform linux/arm64 swr.cn-north-4.myhuaweicloud.com/kunpeng-ai/swerebench-arm64-rancher-sandbox-cluster-api-provider-rke2:115-e9f3693
        docker pull --platform linux/arm64 swr.cn-north-4.myhuaweicloud.com/kunpeng-ai/swerebench-arm64-rs-zerolog:663-e5aa7e3
        docker pull --platform linux/arm64 swr.cn-north-4.myhuaweicloud.com/kunpeng-ai/swerebench-arm64-spoonlabs-gumtree-spoon-ast-diff:182-bda772d
        docker pull --platform linux/arm64 swr.cn-north-4.myhuaweicloud.com/kunpeng-ai/swerebench-arm64-tikv-pd:5480-d269010
        docker pull --platform linux/arm64 swr.cn-north-4.myhuaweicloud.com/kunpeng-ai/swerebench-arm64-triggermesh-aws-event-sources:152-12fb626
        docker pull --platform linux/arm64 swr.cn-north-4.myhuaweicloud.com/kunpeng-ai/swerebench-arm64-typescript-eslint-tslint-to-eslint-config:712-56fd915
        docker pull --platform linux/arm64 swr.cn-north-4.myhuaweicloud.com/kunpeng-ai/swerebench-arm64-vaskoz-dailycodingproblem-go:562-ae3c2bd
        docker pull --platform linux/arm64 swr.cn-north-4.myhuaweicloud.com/kunpeng-ai/swerebench-arm64-vaskoz-dailycodingproblem-go:653-6dd851b
        docker pull --platform linux/arm64 swr.cn-north-4.myhuaweicloud.com/kunpeng-ai/swerebench-arm64-vaskoz-dailycodingproblem-go:763-75b96f3
        docker pull --platform linux/arm64 swr.cn-north-4.myhuaweicloud.com/kunpeng-ai/swerebench-arm64-wikidata-wikidata-toolkit:717-e99f196
        ;;
    x86)
        docker pull --platform linux/amd64 swr.cn-north-4.myhuaweicloud.com/kunpeng-ai/swerebench-x86-aftership-clickhouse-sql-parser:128-8bea76f
        docker pull --platform linux/amd64 swr.cn-north-4.myhuaweicloud.com/kunpeng-ai/swerebench-x86-alangpierce-sucrase:800-328cc66
        docker pull --platform linux/amd64 swr.cn-north-4.myhuaweicloud.com/kunpeng-ai/swerebench-x86-alecthomas-kong:169-9c81441
        docker pull --platform linux/amd64 swr.cn-north-4.myhuaweicloud.com/kunpeng-ai/swerebench-x86-apigee-registry:1142-6e76c8a
        docker pull --platform linux/amd64 swr.cn-north-4.myhuaweicloud.com/kunpeng-ai/swerebench-x86-aquasecurity-tfsec:969-8808315
        docker pull --platform linux/amd64 swr.cn-north-4.myhuaweicloud.com/kunpeng-ai/swerebench-x86-auth0-auth0-spa-js:341-6192758
        docker pull --platform linux/amd64 swr.cn-north-4.myhuaweicloud.com/kunpeng-ai/swerebench-x86-bombsimon-wsl:108-bbddf87
        docker pull --platform linux/amd64 swr.cn-north-4.myhuaweicloud.com/kunpeng-ai/swerebench-x86-bombsimon-wsl:173-9bcfb87
        docker pull --platform linux/amd64 swr.cn-north-4.myhuaweicloud.com/kunpeng-ai/swerebench-x86-caarlos0-env:223-863e59d
        docker pull --platform linux/amd64 swr.cn-north-4.myhuaweicloud.com/kunpeng-ai/swerebench-x86-capricorn86-happy-dom:824-fdbfc53
        docker pull --platform linux/amd64 swr.cn-north-4.myhuaweicloud.com/kunpeng-ai/swerebench-x86-ceph-go-ceph:889-4787861
        docker pull --platform linux/amd64 swr.cn-north-4.myhuaweicloud.com/kunpeng-ai/swerebench-x86-cerbos-cerbos:1655-a7d5e37
        docker pull --platform linux/amd64 swr.cn-north-4.myhuaweicloud.com/kunpeng-ai/swerebench-x86-cometbft-cometbft:3045-fec9682
        docker pull --platform linux/amd64 swr.cn-north-4.myhuaweicloud.com/kunpeng-ai/swerebench-x86-containrrr-watchtower:1784-9180e95
        docker pull --platform linux/amd64 swr.cn-north-4.myhuaweicloud.com/kunpeng-ai/swerebench-x86-datreeio-datree:401-799d634
        docker pull --platform linux/amd64 swr.cn-north-4.myhuaweicloud.com/kunpeng-ai/swerebench-x86-editorconfig-checker-editorconfig-checker:126-01235e1
        docker pull --platform linux/amd64 swr.cn-north-4.myhuaweicloud.com/kunpeng-ai/swerebench-x86-fabien0102-ts-to-zod:240-16b5ce6
        docker pull --platform linux/amd64 swr.cn-north-4.myhuaweicloud.com/kunpeng-ai/swerebench-x86-fhir-sushi:439-3cf43a3
        docker pull --platform linux/amd64 swr.cn-north-4.myhuaweicloud.com/kunpeng-ai/swerebench-x86-fhir-sushi:513-709158e
        docker pull --platform linux/amd64 swr.cn-north-4.myhuaweicloud.com/kunpeng-ai/swerebench-x86-formidablelabs-urql:410-f7e207b
        docker pull --platform linux/amd64 swr.cn-north-4.myhuaweicloud.com/kunpeng-ai/swerebench-x86-gavv-httpexpect:230-ad7be84
        docker pull --platform linux/amd64 swr.cn-north-4.myhuaweicloud.com/kunpeng-ai/swerebench-x86-goadesign-goa:3127-5af5c8a
        docker pull --platform linux/amd64 swr.cn-north-4.myhuaweicloud.com/kunpeng-ai/swerebench-x86-goadesign-goa:3497-e41215d
        docker pull --platform linux/amd64 swr.cn-north-4.myhuaweicloud.com/kunpeng-ai/swerebench-x86-googleapis-java-storage:2661-53fbba7
        docker pull --platform linux/amd64 swr.cn-north-4.myhuaweicloud.com/kunpeng-ai/swerebench-x86-hetznercloud-hcloud-go:45-482fc40
        docker pull --platform linux/amd64 swr.cn-north-4.myhuaweicloud.com/kunpeng-ai/swerebench-x86-hipparchus-math-hipparchus:266-1681d1b
        docker pull --platform linux/amd64 swr.cn-north-4.myhuaweicloud.com/kunpeng-ai/swerebench-x86-jhipster-generator-jhipster:9405-637aafb
        docker pull --platform linux/amd64 swr.cn-north-4.myhuaweicloud.com/kunpeng-ai/swerebench-x86-ktsn-vuex-smart-module:243-4edbc13
        docker pull --platform linux/amd64 swr.cn-north-4.myhuaweicloud.com/kunpeng-ai/swerebench-x86-kubernetes-sigs-cloud-provider-azure:892-46f2c54
        docker pull --platform linux/amd64 swr.cn-north-4.myhuaweicloud.com/kunpeng-ai/swerebench-x86-kubernetes-sigs-kwok:144-8c0fd3e
        docker pull --platform linux/amd64 swr.cn-north-4.myhuaweicloud.com/kunpeng-ai/swerebench-x86-lingui-js-lingui:807-cbe946e
        docker pull --platform linux/amd64 swr.cn-north-4.myhuaweicloud.com/kunpeng-ai/swerebench-x86-microsoft-mssql-jdbc:1838-7ffc2f0
        docker pull --platform linux/amd64 swr.cn-north-4.myhuaweicloud.com/kunpeng-ai/swerebench-x86-mmcdole-gofeed:259-6e8dddc
        docker pull --platform linux/amd64 swr.cn-north-4.myhuaweicloud.com/kunpeng-ai/swerebench-x86-moov-io-customers:275-fce1954
        docker pull --platform linux/amd64 swr.cn-north-4.myhuaweicloud.com/kunpeng-ai/swerebench-x86-nats-io-nats-server:4298-37d3220
        docker pull --platform linux/amd64 swr.cn-north-4.myhuaweicloud.com/kunpeng-ai/swerebench-x86-nats-io-nats-server:5783-1e1169b
        docker pull --platform linux/amd64 swr.cn-north-4.myhuaweicloud.com/kunpeng-ai/swerebench-x86-ontodev-robot:1225-27d5fd3
        docker pull --platform linux/amd64 swr.cn-north-4.myhuaweicloud.com/kunpeng-ai/swerebench-x86-openrefine-openrefine:6161-307d8bf
        docker pull --platform linux/amd64 swr.cn-north-4.myhuaweicloud.com/kunpeng-ai/swerebench-x86-openrefine-openrefine:7176-d92d8e5
        docker pull --platform linux/amd64 swr.cn-north-4.myhuaweicloud.com/kunpeng-ai/swerebench-x86-opossum-tool-opossumui:2503-0da6d46
        docker pull --platform linux/amd64 swr.cn-north-4.myhuaweicloud.com/kunpeng-ai/swerebench-x86-ota-meshi-eslint-plugin-regexp:285-0eced9d
        docker pull --platform linux/amd64 swr.cn-north-4.myhuaweicloud.com/kunpeng-ai/swerebench-x86-ota-meshi-eslint-plugin-svelte:302-1349cf7
        docker pull --platform linux/amd64 swr.cn-north-4.myhuaweicloud.com/kunpeng-ai/swerebench-x86-plexsystems-konstraint:275-2113156
        docker pull --platform linux/amd64 swr.cn-north-4.myhuaweicloud.com/kunpeng-ai/swerebench-x86-primefaces-primefaces:8980-d09ef48
        docker pull --platform linux/amd64 swr.cn-north-4.myhuaweicloud.com/kunpeng-ai/swerebench-x86-promptfoo-promptfoo:88-8de6e12
        docker pull --platform linux/amd64 swr.cn-north-4.myhuaweicloud.com/kunpeng-ai/swerebench-x86-rancher-sandbox-cluster-api-provider-rke2:115-e9f3693
        docker pull --platform linux/amd64 swr.cn-north-4.myhuaweicloud.com/kunpeng-ai/swerebench-x86-rs-zerolog:663-e5aa7e3
        docker pull --platform linux/amd64 swr.cn-north-4.myhuaweicloud.com/kunpeng-ai/swerebench-x86-spoonlabs-gumtree-spoon-ast-diff:182-bda772d
        docker pull --platform linux/amd64 swr.cn-north-4.myhuaweicloud.com/kunpeng-ai/swerebench-x86-tikv-pd:5480-d269010
        docker pull --platform linux/amd64 swr.cn-north-4.myhuaweicloud.com/kunpeng-ai/swerebench-x86-triggermesh-aws-event-sources:152-12fb626
        docker pull --platform linux/amd64 swr.cn-north-4.myhuaweicloud.com/kunpeng-ai/swerebench-x86-typescript-eslint-tslint-to-eslint-config:712-56fd915
        docker pull --platform linux/amd64 swr.cn-north-4.myhuaweicloud.com/kunpeng-ai/swerebench-x86-vaskoz-dailycodingproblem-go:562-ae3c2bd
        docker pull --platform linux/amd64 swr.cn-north-4.myhuaweicloud.com/kunpeng-ai/swerebench-x86-vaskoz-dailycodingproblem-go:653-6dd851b
        docker pull --platform linux/amd64 swr.cn-north-4.myhuaweicloud.com/kunpeng-ai/swerebench-x86-vaskoz-dailycodingproblem-go:763-75b96f3
        docker pull --platform linux/amd64 swr.cn-north-4.myhuaweicloud.com/kunpeng-ai/swerebench-x86-wikidata-wikidata-toolkit:717-e99f196
        ;;
esac
