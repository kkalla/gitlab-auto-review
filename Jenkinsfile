// 새 tag 푸시 시 196-server에 배포하는 파이프라인.
//
// 트리거는 폴링이다 — 사내망 Jenkins는 inbound가 막혀 있어 GitLab이 웹훅을
// 쏠 수 없으므로 Jenkins가 당겨온다. Job 설정에서 tag만 잡도록 해야 한다:
//   - Repository URL: https://git.sparklingsoda.ai:8443/vision/gitlab-auto-review.git
//     (사내 GitLab. 이 레포는 origin=GitHub도 있지만 사내망 Jenkins가 공인망을
//      폴링할 이유가 없고 ags-watchtower와도 어긋난다.)
//   - Branch Specifier: refs/tags/*
//   - Advanced > Refspec: +refs/tags/*:refs/remotes/origin/tags/*
// (pollSCM 트리거는 아래 선언으로 첫 빌드 후 자동 등록된다.)
//
// Jenkins 컨테이너는 host docker.sock을 쓰므로 여기서의 compose up은 곧
// 호스트(196-server) 배포다. .env는 레포에 없다 — Secret file credential
// 'gitlab-auto-review-env'를 복사해 쓴다. 값 수정은 Jenkins UI에서 credential
// 재업로드 (Manage Jenkins > Credentials > gitlab-auto-review-env > Update).
// credential 변경은 pollSCM이 감지하지 못하므로 그때는 수동 빌드가 필요하다.
pipeline {
    agent any
    options {
        disableConcurrentBuilds()
        timestamps()
    }
    triggers {
        pollSCM('H/3 * * * *')
    }
    stages {
        stage('Deploy') {
            steps {
                // -p로 프로젝트명 고정 — 워크스페이스 디렉토리명에 따라
                // 컨테이너가 중복 생성되는 것을 막는다.
                withCredentials([file(credentialsId: 'gitlab-auto-review-env', variable: 'REVIEWER_ENV')]) {
                    sh '''
                        cp "$REVIEWER_ENV" .env
                        docker compose -p gitlab-auto-review up -d --build
                    '''
                }
            }
        }
        stage('Verify') {
            steps {
                // 부팅 직후 crash-loop(토큰 오류·필수 env 누락 등)를 걸러내는
                // 최소 검증. 이 레포는 부팅 시 GITLAB_URL 스킴과 필수 env를
                // 검증해 즉시 종료하므로 crash-loop이 정확히 잡힌다.
                //
                // 컨테이너명을 하드코딩하지 않고 compose에게 묻는다 — 하드코딩하면
                // 서비스가 늘 때 여기가 조용히 낡는다. 개수 대조는 -p 오타로
                // 0개를 보고 통과하는 공허한 성공을 막는다.
                //
                // RestartCount를 함께 보는 이유: 부팅 후 10초 동안은 크래시루프도
                // "running"으로 보인다(restart: unless-stopped가 즉시 되살리므로).
                // 재시작 1회면 실패로 본다 — 재배포는 항상 컨테이너를 재생성하므로
                // 정상 배포의 카운트는 0이다.
                sh '''
                    sleep 10
                    expected=$(docker compose -p gitlab-auto-review config --services | wc -l)
                    cids=$(docker compose -p gitlab-auto-review ps -aq)
                    actual=$(printf '%s' "$cids" | grep -c . || true)
                    if [ "$actual" -ne "$expected" ]; then
                        echo "FAIL: 컨테이너 $actual개 / compose 서비스 $expected개"
                        exit 1
                    fi
                    rc=0
                    for cid in $cids; do
                        name=$(docker inspect -f '{{.Name}}' "$cid")
                        state=$(docker inspect -f '{{.State.Status}}' "$cid")
                        restarts=$(docker inspect -f '{{.RestartCount}}' "$cid")
                        if [ "$state" != "running" ] || [ "$restarts" -gt 0 ]; then
                            echo "FAIL:$name state=$state restarts=$restarts"
                            docker logs --tail 30 "$cid" 2>&1 || true
                            rc=1
                        fi
                    done
                    exit $rc
                '''
            }
        }
    }
    post {
        always {
            // 시크릿이 워크스페이스에 남지 않게
            sh 'rm -f .env'
        }
        success {
            // ponytail: `docker image prune -f`(비-`-a`)는 dangling(태그 없는) 이미지만
            // 지운다 — 이 서비스의 태그된 이미지나 실행 중 컨테이너가 참조하는 레이어는
            // 안 건드린다. 다만 dangling 이미지는 호스트 전역 스코프라, 196-server가
            // 다른 Jenkins job과 공유되면 그쪽의 미태그 빌드 캐시도 같이 지워질 수 있다.
            // `docker compose build`가 이미지에 com.docker.compose.project 라벨을 남기는지는
            // compose 버전에 따라 갈린다(2.38.2대 회귀 이력 있음, docker/compose#13048) —
            // 이 레포는 196-server의 정확한 compose 버전을 고정하지 않으므로 라벨 필터로
            // 좁히는 건 검증 없이 넣지 않는다. 호스트가 실제로 공유된다고 확인되면 그때
            // compose 버전을 고정하고 라벨 필터를 추가할 것.
            sh 'docker image prune -f'
        }
    }
}
