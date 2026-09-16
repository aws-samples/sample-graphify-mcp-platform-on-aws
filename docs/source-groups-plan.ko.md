# 소스 그룹·교차 소스 관계 그래프 구현 계획

판정: **구현 가능. 기존 소스 추출 결과를 재사용하고, 그룹별로 근거가 있는 관계를 추가하는 방식이 적합하다.** 프론트엔드·백엔드 저장소와 기획·QA 문서를 하나의 프로젝트로 묶고, 관계에서 해당 버전의 원문으로 이동할 수 있도록 설계한다.

이 문서는 초기 조사 결과와 구현 제안이다. 실제 구현은 상시 그룹 서버 대신 Lambda 조회를 사용하고 구성원을 그룹 메타데이터에 원자적으로 저장한다. 현재 기능과 운영 계약은 [소스 그룹 안내](source-groups.md)를 따른다. 아래 경로는 저장소 루트 기준이며, 개발 기간과 초기 한도는 당시 측정 전 추정값이다.

## 1. 현재 기능과 추가로 필요한 부분

| 확인 사항 | 코드 근거 |
|---|---|
| 현재 hub는 활성 public 소스의 그래프를 모아 병합한다. private 소스는 제외한다. 동시 병합은 마지막 쓰기가 이기며 병합 실패는 비치명적으로 처리된다. | `cdk/buildspec.py:534` |
| 폴더/repo/연관 그룹 화면은 기존 노드와 edge를 집계한다. 사용자가 구성원을 정하는 프로젝트 그룹은 없다. | `console/graph.js:309` |
| git은 AST 추출, files/url은 문서 변환 및 선택적 LLM 추출을 수행한다. | `cdk/buildspec.py:244`, `cdk/build_scripts/docs_extract_driver.py:1` |
| 원문 도구는 소스 하나에 고정되어 있다. hub의 원문은 콘솔이 원본 서버로 요청한다. | `runtime/entrypoint.py:62`, `runtime/entrypoint.py:323`, `console/graph.js:1600` |
| graph와 snapshot은 별도로 갱신된다. completion의 build ARN 조건은 DynamoDB 상태 갱신을 보호하지만 S3 발행의 경합까지 막지는 않는다. | `runtime/entrypoint.py:135`, `lambdas/completion/handler.py:39` |

로컬에서 확인한 **graphifyy 0.9.51**의 `merge-graphs`는 ID에 소스 prefix를 붙여 합친 뒤, 다른 repo의 `_callable_class` 선언 중 namespace와 label이 같은 노드를 `same_type_as / INFERRED / confidence_score=0.9`로 연결한다. 제한적인 교차 연결은 이미 있지만, API 호출·요구사항·QA 관계를 일반적으로 추출하는 기능은 아니다.

패키지 근거는 `graphify/cli.py:2514`, `graphify/build.py:2001`, `graphify/cross_repo_types.py:40`이다. 타입·namespace 일치/불일치 및 문서 label 사례의 로컬 메모리 실험으로 동작을 확인했다. 배포된 패키지와의 artifact 동일성은 이번 조사에서 확인하지 않았다.

**주의:** 이 휴리스틱의 `0.9`는 실측 정확도 90%가 아니다. 같은 이름의 타입이 실제로 같은 계약인지 원문으로 검증해야 한다. 또한 현재 병합의 단순 무방향 Graph 표현을 새로운 관계 원본 저장 형식으로 그대로 사용하면 방향·다중 관계가 손실될 수 있다.

## 2. 사용자가 보게 될 기능

예를 들어 `연금 서비스` 그룹에 다음 소스를 추가한다.

| 소스 | 역할 | 간략한 설명 예 |
|---|---|---|
| pension-web | frontend | 고객의 연금 가입·조회 화면. pension-api의 HTTP API를 호출한다. |
| pension-api | backend | 가입 및 계좌 조회 API. 기획서의 REQ 번호를 구현한다. |
| 연금 서비스 기획서 | planning | 가입 조건과 예외 처리 규칙. 요구사항 ID를 포함한다. |
| QA 시나리오 | qa | API 및 화면별 정상·오류 테스트와 요구사항 ID를 포함한다. |

등록 화면에는 선택적인 소스 설명을 추가한다. 그룹에 넣을 때 역할과 그룹 안에서의 설명을 보완하도록 안내하되, 설명이 없는 소스도 등록할 수 있다. 초기 제안은 소스·구성원 설명 500자, 그룹 설명 1,000자다.

그룹 화면에서는 `기획 요구사항 → 구현 API → 프론트엔드 호출 → QA 테스트`를 탐색하고 연결을 선택하면 양쪽의 파일·위치·인용문을 확인한다. 이 경로는 **목표 동작의 예시**이며 현재 플랫폼에서 이미 검증된 결과가 아니다.

Description은 후보를 찾는 단서다. “이 백엔드가 이 기능을 구현한다”는 설명만으로 구현 관계를 사실로 확정하지 않는다.

## 3. 권장 구조와 데이터 모델

**기존 소스 그래프 + 그룹별 관계 파일 + 조회용 그래프**로 구성한다. 원본 소스의 그래프를 다시 추출하거나 덮어쓰지 않고 관계를 추가한다. 그룹은 별도 비공개 리소스로 두고 public hub 및 기존 repo poller에 자동 편입하지 않는다.

기존 RegistryTable과 PlatformTable의 `pk/sk`를 확장한다. 다음 필드는 신규 제안이다.

| 위치 | 주요 필드 |
|---|---|
| source registry row | `description`, `description_version`, `source_epoch`, `acl_epoch`, `active_source_version` |
| 사용자 source grant | `personal_description` |
| `GROUP#gid / META` | `owner_sub`, `name`, `description`, `revision`, `membership_version`, `context_version`, `acl_version`, `active_graph_version`, `status` |
| `GROUP#gid / SOURCE#sid` | `role`, `description_override`, `description_version` |
| `USER#sub / GROUP#gid` | `role=owner/editor/viewer`, `status` |
| `SOURCE#sid / GROUP#gid` | 소스 변경 시 갱신할 그룹의 역참조 |
| `GROUP#gid / BUILD#bid` | 입력 manifest, fencing token, 상태, 사용량 |

소스 공통 설명은 원본 생성자가 관리하며, 소유자가 없는 운영자 등록 소스는 기존 `_can_manage` 규칙을 따른다. 공개 소스의 공통 설명은 공개 정보로 취급한다. 구독자 개인 설명은 공유하지 않는다. 그룹별 설명은 같은 소스가 각 프로젝트에서 맡는 역할을 표현한다.

관계 분석에는 그룹 설명·그룹별 소스 설명·공통 설명을 구분해 전달한다. 개인 설명을 자동 복사하지 않는다. 설명 수정은 관계 분석을 무효화하되 원본 추출은 재사용한다.

PlatformTable의 역인덱스는 작업 발견에만 사용하고 권한 판단은 원본 항목을 읽어 수행한다. 기존 엔터티 조회가 신규 그룹 항목을 소스로 오인하지 않는지도 검증한다.

## 4. ID·버전·원문 계약

그룹 ID는 이름과 무관한 `grp_<uuid>`로 고정한다. 노드의 논리 ID는 `(source_id, original_node_id)`이며 표시 이름·Description·community 번호를 식별자로 사용하지 않는다. 노드에는 `source_version`, `source_file`, 원본 위치, 변환된 Markdown 위치를 별도로 둔다.

현재 viz의 ID 512자 절단(`cdk/build_scripts/make_viz.py:445`)을 그룹 machine ID에 적용하지 않는다. 전체 ID 또는 충돌 검증된 opaque ID map을 사용한다.

각 소스 build는 **불변 버전 경로**에 graph·snapshot·위치 map·checksum manifest를 게시한다. 그룹 manifest는 구성 소스의 정확한 버전과 membership/context/추출기 버전을 고정한다. 그룹 artifact 경로는 `groups/<gid>/versions/<version>/`으로 분리한다.

그룹 원문 도구는 다음 입력을 받는다.

```text
read_source(group_version, source_id, node_id 또는 file, start_line?, end_line?)
```

서버가 manifest로 snapshot과 파일을 결정한다. 과거 버전의 snapshot이 없으면 명시적인 버전 오류를 반환하며 latest 원문으로 대체하지 않는다. 현재 2MiB/400줄 제한과 경로 탈출 방어를 유지한다. bucket/key, runtime host, `project_path`를 클라이언트가 지정할 수 없도록 한다.

페이지·Excel 좌표·근사 label 위치와 Markdown 줄 번호를 구별한다. 그룹 결과의 정확한 원문 인용은 관계가 분석된 버전에서 재검증한다.

## 5. API·권한·회수

신규 API 제안:

```text
POST /groups
GET /groups
GET|POST|DELETE /groups/{gid}
POST /groups/{gid}/sources
DELETE /groups/{gid}/sources/{sid}
POST /groups/{gid}/members
DELETE /groups/{gid}/members/{sub}
POST /groups/{gid}/rebuild
GET /groups/{gid}/graph
GET /groups/{gid}/graph/chunks
GET /groups/{gid}/relations/{eid}
POST /groups/{gid}/source
POST /repos/{sid}/description
```

변경 요청에는 `expected_revision`, 생성·빌드에는 idempotency key를 사용한다. 비동기 빌드는 `202`와 build ID를 반환한다. `/servers`에 `kind=group`을 추가하고 MCP 경로, key scope, 그래프 및 Playground 선택기를 확장한다.

접근 조건은 **활성 사용자 ∩ 그룹 grant ∩ 모든 구성 소스의 현재 읽기 권한**이다. 각 소스는 enabled이고 명시적으로 public이거나 사용자에게 source grant가 있어야 한다. 그룹 owner도 원본 소스 권한을 우회하지 않는다.

그룹 초대는 그룹 grant만 생성한다. 비공개 원본의 소유자는 별도로 source 권한을 부여한다. 구성 소스를 추가하면 기존 그룹 사용자가 접근 조건을 잃을 수 있으므로 UI에서 영향을 설명한다. 그룹 권한 회수나 그룹 삭제가 기존 source grant·subscription을 삭제해서는 안 된다.

authorizer, proxy, 관리 API, 두 Playground에 같은 정책을 적용한다. Python 공통 모듈은 Layer 또는 명시적 bundle로 배포하고, Node는 proxy의 내부 사전검사를 사용한다. 실제 tool 호출에서도 다시 검사한다. API-key `ALL`이나 group-only scope도 원본 권한을 우회하지 않는다.

권한은 강한 일관성으로 읽고, 누락·미처리 읽기는 차단한다. 회수 후 다음 신규 요청부터 그룹 데이터 전체를 차단한다. 일부 노드만 화면에서 숨기면 통계·경로·근거에 비공개 정보가 남는다.

현재 presigned URL은 발급 후 최대 300초간 유효하다. 신규 private 그룹의 즉시 재검사가 필요한 데이터는 **매 요청 권한을 검사하는 chunk API**로 제공한다. 브라우저 cache도 접근 거절 시 비운다. 이미 다운로드한 데이터의 소급 회수는 보장할 수 없다.

## 6. 관계 분석과 검증

관계 후보를 만드는 순서는 **명시적 연결 → 후보 검색 → 선택적 LLM 판정**이다.

1. 요구사항·test case ID, 파일·symbol 링크, OpenAPI operation, 서비스+HTTP method+route, 메시지 계약을 수집한다.
2. 서로 다른 소스 사이에서 일치 가능성이 있는 후보만 고른다. 모든 노드 쌍 비교는 하지 않는다.
3. LLM을 켠 경우 제한된 후보의 양쪽 원문과 설명을 전달한다.
4. 인용과 관계 스키마를 검증한 결과만 게시한다.

경로나 타입 이름이 같다는 이유만으로 `calls_api` 또는 `implements_requirement`를 확정하지 않는다. 원문과 설명에 포함된 지시문은 실행하지 않으며, 추출기의 지시와 명확히 분리한다.

관계 원본은 버전별 `relations.jsonl`에 저장한다.

```text
edge_id
from/to: source_id, node_id, source_version
relation
evidence_kind, confidence_score
evidence[]: file, version, range, quote, quote_hash
method, model_id, prompt_version, context_version, review_status
```

검증기는 endpoint 존재, 구성 소스 여부, 실제 인용 일치, 위치 범위, 허용 relation, 중복을 확인한다. 확인되지 않은 관계는 확정 관계로 게시하지 않는다. `INFERRED` 점수와 사람의 검토 여부를 별도로 표시하며 `EXTRACTED` 역시 자동으로 사실 판정하지 않는다.

다중·양방향 관계는 원본 관계 파일에 보존한다. graphify용 표현에서 축약할 경우 relation ID와 상세 도구로 원본 관계를 제공한다. viz v2에는 edge ID·근거 종류·점수·검토 상태를 추가한다. 기존 소스 내부 관계는 덮어쓰지 않는다.

## 7. 변경·경합·삭제

그룹마다 변경 이벤트를 모아 실행하고, 동시에 하나의 유효 빌드만 발행하도록 single-flight와 fencing token을 사용한다. 시작 시 입력 manifest를 고정하고, 발행 직전 group revision과 source version/epoch를 조건부 검사한다. 늦은 빌드는 active pointer를 바꾸지 못한다.

| 변경 | 처리 |
|---|---|
| 소스 내용 변경 | 해당 소스가 참여하는 후보와 관계를 재평가한다. |
| 구성 추가·제거 | membership version을 높이고 이전 구성 결과의 제공을 차단한다. |
| 그룹 안의 소스 설명 변경 | 관련 소스 쌍을 재평가한다. |
| 그룹 설명 변경 | 관계 전체를 재평가한다. |
| 권한 회수·소스 disabled | 읽기를 먼저 차단하고 취소·정리는 비동기로 수행한다. |
| 그룹·소스 삭제 | tombstone과 epoch로 늦은 결과의 재게시를 막는다. |

최신 revision으로 재실행하며 주기적 reconciliation으로 이벤트 유실을 복구한다. 구성 제거 시 노드·관계·근거를 함께 제외한다. 그룹 삭제는 원본 소스를 삭제하지 않는다. 같은 source ID를 재등록해도 새 epoch로 구분한다.

활성 manifest가 참조하는 snapshot은 참조가 끝날 때까지 보존한다. 현재 `repos/` noncurrent 7일 및 `history/` 30일 만료를 그룹의 버전 보존 정책으로 그대로 사용하지 않는다.

## 8. 비용과 초기 한도

그룹 생성만으로 Fargate runtime을 추가하지 않는다. 그룹 관리·설명과 분석·MCP runtime 활성화를 분리한다. 초기 그룹 전용 runtime은 구현이 단순하지만 고정비가 늘어나므로 선택적으로 활성화한다.

초기 검증용 한도 제안:

| 항목 | 제안값 |
|---|---:|
| 구성 소스 | 8개/그룹 |
| 조회용 그래프 | 32MiB, 5만 노드, 20만 edge |
| 관계 후보 | 1,000쌍/빌드 |
| LLM 입출력 | 입력 20만·출력 4만 tokens/빌드 |
| LLM 동시 호출 | 2개 |
| 관계 빌드 | 15분 |
| 그래프 chunk | 1MiB |

이 값은 성능이나 정확도를 보장하는 측정 결과가 아니다. 0단계 실험에서 조정한다. token은 호출 전에 예약하고 재시도까지 계상한다. 한도 초과·부분 완료를 `PARTIAL/BUDGET_EXCEEDED`로 표시하며 완전한 분석처럼 제공하지 않는다.

관계 cache key에 양쪽 소스·근거 hash, 모델, prompt, Description 버전을 포함한다. private 결과를 다른 그룹과 공유하지 않는다. 소스 추가마다 전체 문서를 다시 AI 추출하지 않고 기존 그래프와 변경된 후보를 재사용한다.

비용은 **runtime 시간 + CodeBuild 시간 + 모델 입출력 + 저장·전송 + 권한 조회**로 보고한다. 기존 문서의 약 $17/월/runtime은 과거 참고값이며 현재 견적이 아니다. 최신 단가, 메모리 사용량, reload 비용은 구현 검증에서 별도로 측정한다.

## 9. 구현 순서와 완료 기준

| 단계 | 주요 변경 파일 | 완료 기준 | 예상 작업일 |
|---|---|---|---:|
| 0. 계약 실험 | 신규 `tests/test_group_graph_contract.py` | 패키지 hash 기록, ID·방향·다중 관계·hyperedge·MCP 호환 fixture | 2 |
| 1. 그룹 관리·권한 | `lambdas/platform_api/`, 공통 접근 모듈, `cdk/graphify_stack.py`, `console/index.html` | 설명 격리, revision 충돌, 초대 시 권한 확대 방지 | 4–5 |
| 2. 버전 발행·조회 | `cdk/buildspec.py`, 신규 그룹 buildspec/coordinator, completion, runtime, runtimes, authorizer/proxy | 불변 manifest, 역순 완료 방어, 해당 버전 원문 조회 | 5–7 |
| 3. 명시적 관계·UI | 신규 관계 helper, `make_viz.py`, `console/graph.js`·`graph.css`, 두 Playground | 기획→구현→QA 근거 탐색 및 권한 회수 | 4–5 |
| 4. 선택적 LLM | 후보·판정 helper, 예산·검토 UI | 원문 인용 검증, 예산 제어, 한국어 관계 평가 | 4–6 |
| 5. 운영 검증 | 그룹 smoke, reference, 양쪽 README | 경합·삭제·부하·비용·rollback 검증 | 2–3 |

총 **21–28 개발 작업일**, 배포 대기 제외 추정이다. 신규 파일은 제안이며 이 계획 문서 외에는 아직 생성하지 않았다. 0–3단계로 관계 분석에서 LLM을 사용하지 않는 MVP를 먼저 검증하고, 관계 정밀도·재현율과 비용을 측정한 뒤 4단계를 활성화하는 것을 권장한다. 기존 문서 소스의 AI 추출 설정은 유지하며 이미 추출된 결과를 재사용한다.

필수 수용 테스트:

- 동일 ID·README 경로·label을 가진 frontend/backend 노드가 충돌하지 않는다.
- 명시적인 요구사항→API→QA 정답 관계를 복구하고 양쪽의 정확한 인용을 읽는다.
- 원문에 근거가 없는 Description만으로 구현 관계를 확정하지 않는다.
- A만 접근 가능한 사용자는 A+B 그룹의 데이터·통계·근거를 읽지 못한다.
- hub/source/group-only/ALL key와 두 Playground에서 같은 회수 정책을 적용한다.
- v1/v2 역순 완료, 빌드 중 구성 삭제·권한 회수·그룹 삭제가 재노출을 만들지 않는다.
- graph v1은 source v1을 읽거나 버전 오류를 반환한다.
- 동시 token 예약, 재시도, 부분 실패가 예산 제한을 우회하지 못한다.

주요 위험은 잘못된 관계 연결, 관계 표현의 정보 손실, 문서 위치 부정확성, artifact 버전 혼합, 파생 정보의 권한 잔존, runtime 고정비다. 위 테스트와 단계별 측정으로 출시 여부를 판단한다.
