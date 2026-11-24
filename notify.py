#!/usr/bin/env python3
"""GitHub 알림 중 중요한 것만 골라 Slack 봇 채널(Incoming Webhook)로 보낸다.

거르는 기준 (README.md에 이유를 적어 둠)
- 리뷰 요청·담당 지정·보안 경고·CI 실패: 처음 한 번은 바로 보낸다.
- 그 밖의 알림: 지난 확인 뒤에 "나도 봇도 아닌 사람"이 댓글·리뷰를 남겼을 때만 보낸다.
- Watch로 오는 알림(subscribed)은 보내지 않는다.
- GitHub 웹에서 이미 읽은 알림은 보내지 않는다.

사용법
  notify.py                 # 한 번 확인하고 보낸다 (launchd가 5분마다 부른다)
  notify.py --dry-run       # 보내지 않고 화면에 찍기만 한다. 상태도 저장하지 않는다
  notify.py --dry-run --since 3d   # 3일치를 대상으로 시험
"""
import argparse
import datetime as dt
import json
import os
import subprocess
import sys
import urllib.request

HOME = os.environ.get("GH_NOTIFY_HOME") or os.path.expanduser("~/.thakicloud/gh-notify")
STATE = os.path.join(HOME, "state.json")
CONFIG = os.path.join(HOME, "config.json")
LOG = os.path.join(HOME, "logs", "run.log")

# 처음 한 번은 사람 활동이 없어도 보내는 이유
ALWAYS_ONCE = {
    "review_requested": "리뷰 요청",
    "assign": "담당 지정",
    "security_alert": "보안 경고",
    "ci_activity": "CI 실패",
}
# 아예 보내지 않는 이유
SKIP = {"subscribed"}
# 사람이 아무리 오래 쉬어도 이보다 오래된 알림은 다시 보내지 않는다
MAX_LOOKBACK = dt.timedelta(hours=24)


def now():
    return dt.datetime.now(dt.timezone.utc)


def iso(t):
    return t.strftime("%Y-%m-%dT%H:%M:%SZ")


def parse(s):
    return dt.datetime.strptime(s, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=dt.timezone.utc)


def log(msg):
    os.makedirs(os.path.dirname(LOG), exist_ok=True)
    kst = dt.datetime.now(dt.timezone(dt.timedelta(hours=9))).strftime("%Y-%m-%d %H:%M:%S")
    with open(LOG, "a") as f:
        f.write(f"{kst} {msg}\n")


def gh(path, paginate=False):
    cmd = ["gh", "api", "-H", "Accept: application/vnd.github+json", path]
    if paginate:
        cmd.insert(2, "--paginate")
        cmd += ["--jq", ".[]"]
    r = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
    if r.returncode != 0:
        raise RuntimeError(f"gh api {path}: {r.stderr.strip()[:300]}")
    if paginate:
        return [json.loads(line) for line in r.stdout.splitlines() if line.strip()]
    return json.loads(r.stdout) if r.stdout.strip() else None


def is_bot(user):
    if not user:
        return True
    return user.get("type") == "Bot" or user.get("login", "").endswith("[bot]")


def human_activity(n, since, me):
    """since 뒤에 나도 봇도 아닌 사람이 남긴 댓글·리뷰를 시간순으로 돌려준다."""
    subj = n["subject"]
    url = subj.get("url") or ""
    if subj["type"] not in ("Issue", "PullRequest") or not url:
        return []
    repo = n["repository"]["full_name"]
    num = url.rstrip("/").split("/")[-1]
    s = iso(since)
    acts = []
    for c in gh(f"repos/{repo}/issues/{num}/comments?since={s}&per_page=100", paginate=True):
        acts.append(("comment", c["user"], c["created_at"], c.get("body") or "", c["html_url"]))
    if subj["type"] == "PullRequest":
        for r in gh(f"repos/{repo}/pulls/{num}/reviews?per_page=100", paginate=True):
            if r.get("submitted_at") and r["submitted_at"] > s:
                acts.append((f"review:{r['state']}", r["user"], r["submitted_at"], r.get("body") or "", r["html_url"]))
        for c in gh(f"repos/{repo}/pulls/{num}/comments?since={s}&per_page=100", paginate=True):
            acts.append(("inline", c["user"], c["created_at"], c.get("body") or "", c["html_url"]))
    acts = [a for a in acts if a[2] > s and not is_bot(a[1]) and a[1]["login"] != me]
    return sorted(acts, key=lambda a: a[2])


def html_url(n):
    repo = n["repository"]["html_url"]
    url = n["subject"].get("url") or ""
    num = url.rstrip("/").split("/")[-1]
    kind = {"PullRequest": "pull", "Issue": "issues"}.get(n["subject"]["type"])
    if kind and num.isdigit():
        return f"{repo}/{kind}/{num}", num
    return f"{repo}", ""


def first_line(text, limit=140):
    for line in text.splitlines():
        line = line.strip().lstrip(">#*- ").strip()
        if line and not line.startswith("<!--"):
            return line if len(line) <= limit else line[: limit - 1] + "…"
    return ""


def label_for(n, acts, me):
    reason = n["reason"]
    if acts:
        states = [a[0] for a in acts]
        if "review:CHANGES_REQUESTED" in states:
            return "변경 요청"
        if "review:APPROVED" in states:
            return "승인"
        if any(f"@{me}".lower() in a[3].lower() for a in acts):
            return "멘션"
        if reason in ("author",):
            return "내 글에 댓글"
        if reason == "team_mention":
            return "팀 멘션"
        return "새 댓글"
    return ALWAYS_ONCE.get(reason, reason)


def build_message(n, label, acts):
    url, num = html_url(n)
    repo = n["repository"]["name"]
    ref = f"{repo}#{num}" if num else repo
    title = n["subject"]["title"].replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
    lines = [f"*[{label}]* <{url}|{ref}> {title}"]
    for kind, user, _, body, link in acts[-3:]:
        text = first_line(body).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
        if kind.startswith("review:"):
            text = {"APPROVED": "승인했습니다", "CHANGES_REQUESTED": "변경을 요청했습니다"}.get(kind[7:], text or "리뷰를 남겼습니다")
        lines.append(f"> <{link}|{user['login']}>: {text}")
    if len(acts) > 3:
        lines.append(f"_외 {len(acts) - 3}건_")
    return "\n".join(lines)


def post(webhook, text):
    data = json.dumps({"text": text, "unfurl_links": False, "unfurl_media": False}).encode()
    req = urllib.request.Request(webhook, data=data, headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=20) as r:
        if r.status != 200:
            raise RuntimeError(f"Slack {r.status}")


def load(path, default):
    try:
        with open(path) as f:
            return json.load(f)
    except FileNotFoundError:
        return default


# 처리 기록은 Gist 하나에 둔다. Mac과 GitHub Actions가 같은 기록을 읽고 써서 같은 알림을 두 번 보내지 않는다.
# gist_id가 없으면 예전처럼 로컬 state.json을 쓴다.
GIST_ID = None
# Mac이 이 시간 안에 돌았으면 Actions(백업)는 아무것도 하지 않는다
MAC_FRESH = dt.timedelta(minutes=15)


def load_state():
    if not GIST_ID:
        return load(STATE, {})
    content = subprocess.run(
        ["gh", "api", f"gists/{GIST_ID}", "--jq", '.files["state.json"].content'],
        capture_output=True, text=True, timeout=60,
    )
    if content.returncode != 0:
        raise RuntimeError(f"Gist 읽기 실패: {content.stderr.strip()[:200]}")
    return json.loads(content.stdout) if content.stdout.strip() else {}


def save(state):
    if not GIST_ID:
        tmp = STATE + ".tmp"
        with open(tmp, "w") as f:
            json.dump(state, f, ensure_ascii=False, indent=1)
        os.replace(tmp, STATE)
        return
    body = json.dumps({"files": {"state.json": {"content": json.dumps(state, ensure_ascii=False, indent=1)}}})
    r = subprocess.run(["gh", "api", "-X", "PATCH", f"gists/{GIST_ID}", "--input", "-"],
                       input=body, capture_output=True, text=True, timeout=60)
    if r.returncode != 0:
        raise RuntimeError(f"Gist 쓰기 실패: {r.stderr.strip()[:200]}")


def main():
    global GIST_ID
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--since", help="예: 3d, 12h. 상태 파일 대신 이 범위를 본다")
    ap.add_argument("--role", choices=["mac", "backup"], default="mac",
                    help="backup은 Mac이 최근 15분 안에 돌았으면 건너뛴다 (GitHub Actions용)")
    ap.add_argument("--force", action="store_true", help="backup이어도 Mac 실행 여부와 상관없이 돈다 (시험용)")
    args = ap.parse_args()

    cfg = load(CONFIG, {})
    GIST_ID = os.environ.get("GH_NOTIFY_GIST") or cfg.get("gist_id")
    webhook = os.environ.get("GH_NOTIFY_WEBHOOK") or cfg.get("webhook_url")
    if not webhook and not args.dry_run:
        log("webhook_url이 없어 건너뜀 (config.json)")
        return 0

    t_now = now()
    try:
        state = load_state()
    except Exception as e:
        log(str(e))
        print("처리 기록을 읽지 못해 건너뜀")
        return 1
    if args.role == "backup" and not args.force:
        mac = state.get("mac_seen_at")
        if mac and t_now - parse(mac) < MAC_FRESH:
            print(f"Mac이 {mac}에 돌았으므로 건너뜀")
            return 0
    if args.since:
        unit = {"d": "days", "h": "hours", "m": "minutes"}[args.since[-1]]
        cursor = t_now - dt.timedelta(**{unit: int(args.since[:-1])})
    else:
        cursor = parse(state["cursor"]) if state.get("cursor") else t_now - dt.timedelta(minutes=10)
        cursor = max(cursor, t_now - MAX_LOOKBACK)
    seen = state.get("seen", {})       # thread id -> 마지막으로 처리한 updated_at
    sent_once = set(state.get("sent_once", []))  # "thread id:reason"

    try:
        me = state.get("me") or gh("user")["login"]
        notes = gh(f"notifications?all=true&since={iso(cursor - dt.timedelta(minutes=2))}&per_page=50", paginate=True)
    except Exception as e:
        log(f"GitHub 조회 실패: {e}")
        print("GitHub 조회 실패")
        day = t_now.strftime("%Y-%m-%d")
        if webhook and not args.dry_run and state.get("alerted_fail") != day:
            try:
                post(webhook, f":warning: GitHub 알림을 가져오지 못했습니다. `gh auth status`를 확인해 주세요.\n`{str(e)[:200]}`")
                state["alerted_fail"] = day
                save(state)
            except Exception as e2:
                log(f"실패 알림도 못 보냄: {e2}")
        return 1

    sent = skipped = 0
    failed = False
    for n in sorted(notes, key=lambda x: x["updated_at"]):
        tid, upd, reason = n["id"], n["updated_at"], n["reason"]
        if seen.get(tid) == upd:
            continue
        last_read = n.get("last_read_at")
        prev = seen.get(tid)
        if reason in SKIP or (last_read and last_read >= upd):
            seen[tid] = upd
            skipped += 1
            continue
        since_marks = [cursor - dt.timedelta(minutes=2)]
        if prev:
            since_marks.append(parse(prev))
        if last_read:
            since_marks.append(parse(last_read))
        since = max(since_marks) if not args.since else max([cursor] + ([parse(last_read)] if last_read else []))
        try:
            acts = human_activity(n, since, me)
        except Exception as e:
            log(f"활동 조회 실패 {n['repository']['full_name']} {n['subject']['title'][:40]}: {e}")
            continue  # seen을 안 남겨 다음에 다시 본다
        once_key = f"{tid}:{reason}"
        first_request = reason in ALWAYS_ONCE and once_key not in sent_once
        if not acts and not first_request:
            seen[tid] = upd
            skipped += 1
            continue
        text = build_message(n, label_for(n, acts, me), acts)
        if args.dry_run:
            print(text, end="\n\n")
        else:
            try:
                post(webhook, text)
            except Exception as e:
                log(f"Slack 전송 실패: {e}")
                failed = True
                break  # 확인 시각을 앞으로 옮기지 않아 다음 실행 때 다시 보낸다
        if first_request:
            sent_once.add(once_key)
        seen[tid] = upd
        sent += 1

    summary = f"[{args.role}] 알림 {len(notes)}건 중 보냄 {sent} · 거름 {skipped}" + (" · Slack 실패" if failed else "")
    log(("[dry-run] " if args.dry_run else "") + summary)
    if not args.dry_run:
        print(summary)  # Actions 로그는 공개되므로 건수만 찍는다
    if args.dry_run:
        print(f"-- 알림 {len(notes)}건 중 보낼 것 {sent} · 거른 것 {skipped}")
        return 0
    # 상태 파일이 끝없이 커지지 않게 3일 넘은 기록은 지운다
    old = iso(t_now - dt.timedelta(days=3))
    if not failed:
        state["cursor"] = iso(t_now)
    if args.role == "mac":
        state["mac_seen_at"] = iso(t_now)
    state.update(
        last_run=f"{args.role} {iso(t_now)}",
        me=me,
        seen={k: v for k, v in seen.items() if v >= old},
        sent_once=sorted(sent_once)[-500:],
    )
    try:
        save(state)
    except Exception as e:
        log(str(e))
        print("처리 기록을 저장하지 못함")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
