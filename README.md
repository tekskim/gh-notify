# gh-notify

GitHub 알림 가운데 중요한 것만 골라 Slack 채널(Incoming Webhook)로 보낸다.

평소에는 Mac의 launchd가 5분마다 `notify.py`를 실행한다.
Mac이 잠들어 있을 때는 이 저장소의 GitHub Actions가 대신 실행한다.
두 쪽이 처리 기록을 비공개 Gist 하나에 같이 적기 때문에 같은 알림을 두 번 보내지 않는다.

## 보내는 알림

- 리뷰 요청, 담당 지정, 보안 경고, CI 실패: 처음 온 때 한 번 보낸다.
- 그 밖의 알림: 지난 확인 뒤 나도 봇도 아닌 사람이 댓글이나 리뷰를 남겼을 때만 보낸다.
- Watch로 오는 알림과 GitHub 웹에서 이미 읽은 알림은 보내지 않는다.

## Actions secret

| 이름 | 내용 |
|---|---|
| `GH_NOTIFY_TOKEN` | classic 개인 토큰. `repo`, `gist` 권한 |
| `GH_NOTIFY_GIST` | 처리 기록을 둔 비공개 Gist의 ID |
| `SLACK_WEBHOOK` | Slack Incoming Webhook 주소 |
