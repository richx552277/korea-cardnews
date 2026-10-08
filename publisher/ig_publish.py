"""인스타그램 클라우드 예약 게시 — GitHub Actions가 15분마다 실행한다 (2026-10-08 대표 결정, PC가 꺼져 있어도 게시).

원본: E:\\claude\\instagram-agency\\common\\cloud\\ig_publish.py → 게시 저장소(korea-cardnews)의 publisher/ 로 배포(cloud_queue.py deploy).
표준 라이브러리만 쓴다. 토큰은 저장소 Secrets(채널 token_env 이름)에서 환경변수로 받는다 — 출력·저장하지 않는다.

  python ig_publish.py            게시 시각이 지난 일정을 게시하고 결과를 published.json에 남긴다
  python ig_publish.py --dry      무엇을 게시할지만 출력
  python ig_publish.py --check    Secret 토큰이 각 계정과 맞는지만 확인 (게시 안 함)

schedule.json  (PC의 cloud_queue.py가 만든다) {"items": [{id, channel, handle, token_env, kind, due, caption, video_url, cover_ms, image_urls, alt}]}
published.json (이 스크립트가 쓴다)          {"<id>": {status: published|failed|missed|duplicate, at, permalink, media_id, error, attempts}}
규칙
  - due가 지나고 3시간 안인 항목만 게시. 3시간이 지나면 missed — PC가 다음 빈 날로 다시 잡는다
  - 채널당 하루(KST) 1개 — 같은 날 이미 게시한 채널은 duplicate
  - 실패는 3번까지 다음 실행에서 다시 시도
  - 게시 직전 최근 게시물 캡션을 확인해 이미 올라간 것이면 다시 올리지 않는다(결과 기록 실패 대비)
"""
import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

HERE = Path(__file__).resolve().parent
SCHEDULE = HERE / "schedule.json"
RESULTS = HERE / "published.json"
IG_API = "https://graph.instagram.com/v23.0"
KST = timezone(timedelta(hours=9))
LATE_LIMIT = timedelta(hours=3)
MAX_ATTEMPTS = 3


class IGError(Exception):
    pass


def http(method, url, data=None, timeout=60):
    headers = {"Content-Type": "application/x-www-form-urlencoded; charset=utf-8"} if data else {}
    req = urllib.request.Request(url, data=data, method=method, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, json.loads(r.read() or b"{}")
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode("utf-8", "replace")


def ig(method, path, token, **fields):
    fields["access_token"] = token
    if method == "GET":
        code, body = http("GET", f"{IG_API}/{path}?{urllib.parse.urlencode(fields)}")
    else:
        code, body = http("POST", f"{IG_API}/{path}", urllib.parse.urlencode(fields).encode("utf-8"))
    if code != 200:
        raise IGError(f"{path}: {str(body)[:300]}")
    return body


def wait_container(cid, token, tries=60, gap=5):
    s = {}
    for _ in range(tries):
        s = ig("GET", cid, token, fields="status_code,status")
        if s.get("status_code") in ("FINISHED", "ERROR", "EXPIRED"):
            break
        time.sleep(gap)
    if s.get("status_code") != "FINISHED":
        raise IGError(f"처리 실패: {s}")


def already_posted(uid, token, caption):
    """최근 게시물 중 캡션 앞부분이 같은 것이 있으면 그 게시물 (결과 기록이 실패했던 경우 중복 방지)"""
    head = caption.strip()[:80]
    media = ig("GET", f"{uid}/media", token, fields="id,caption,permalink,timestamp", limit="10").get("data", [])
    return next((m for m in media if (m.get("caption") or "").strip()[:80] == head), None)


def post(e, token):
    me = ig("GET", "me", token, fields="user_id,username")
    if me["username"].lower() != e["handle"].lower():
        raise IGError(f"@{e['handle']} 토큰이 아님(@{me['username']})")
    uid = me["user_id"]
    dup = already_posted(uid, token, e["caption"])
    if dup:
        return dup["id"], dup["permalink"], "이미 게시돼 있음(기록만)"
    if e["kind"] == "reel":
        c = ig("POST", f"{uid}/media", token, media_type="REELS", video_url=e["video_url"], caption=e["caption"],
               share_to_feed="true", thumb_offset=str(e.get("cover_ms", 1500)))
        wait_container(c["id"], token)
    else:
        ids = []
        alts = e.get("alt") or []
        for i, u in enumerate(e["image_urls"]):
            f = {"image_url": u, "is_carousel_item": "true"}
            if i < len(alts) and alts[i]:
                f["alt_text"] = alts[i]
            try:
                ids.append(ig("POST", f"{uid}/media", token, **f)["id"])
            except IGError:
                if "alt_text" not in f:
                    raise
                f.pop("alt_text")  # 캐러셀 항목이 대체텍스트를 거부하면 빼고 다시
                ids.append(ig("POST", f"{uid}/media", token, **f)["id"])
        c = ig("POST", f"{uid}/media", token, media_type="CAROUSEL", children=",".join(ids), caption=e["caption"])
        wait_container(c["id"], token, tries=40, gap=3)
    p = ig("POST", f"{uid}/media_publish", token, creation_id=c["id"])
    m = ig("GET", p["id"], token, fields="permalink")
    return p["id"], m["permalink"], ""


def check_tokens():
    """Secret마다 토큰이 있고 기대한 계정인지만 확인 (게시 안 함) — 값은 출력하지 않는다"""
    want = json.loads(SCHEDULE.read_text(encoding="utf-8")).get("accounts", {}) if SCHEDULE.exists() else {}
    bad = 0
    for env, handle in sorted(want.items()):
        token = "".join(ch for ch in os.environ.get(env, "") if ch.isprintable() and not ch.isspace())
        if not token:
            print(f"✗ {env}: Secret 없음")
            bad += 1
            continue
        try:
            me = ig("GET", "me", token, fields="username")
            ok = me["username"].lower() == handle.lower()
            print(f"{'✓' if ok else '✗'} {env}: @{me['username']}" + ("" if ok else f" (기대 @{handle})"))
            bad += 0 if ok else 1
        except IGError as ex:
            print(f"✗ {env}: {str(ex)[:200]}")
            bad += 1
    if not want:
        print("schedule.json에 accounts 없음")
    sys.exit(1 if bad or not want else 0)


def main():
    if "--check" in sys.argv:
        return check_tokens()
    dry = "--dry" in sys.argv
    items = json.loads(SCHEDULE.read_text(encoding="utf-8")).get("items", []) if SCHEDULE.exists() else []
    res = json.loads(RESULTS.read_text(encoding="utf-8")) if RESULTS.exists() else {}
    now = datetime.now(timezone.utc)
    stamp = now.astimezone(KST).isoformat(timespec="seconds")
    changed = False
    for e in sorted(items, key=lambda x: x["due"]):
        r = res.get(e["id"], {})
        if r.get("status") in ("published", "missed", "duplicate") or r.get("attempts", 0) >= MAX_ATTEMPTS:
            continue
        due = datetime.fromisoformat(e["due"])
        if due > now:
            continue
        day = due.astimezone(KST).date().isoformat()
        same_day = [k for k, v in res.items() if v.get("status") == "published" and v.get("channel") == e["channel"]
                    and (v.get("due") or "")[:10] == day]
        if same_day:
            res[e["id"]] = {"status": "duplicate", "at": stamp, "channel": e["channel"], "due": e["due"], "note": f"같은 날 {same_day[0]} 게시됨"}
            changed = True
            print(f"[{e['channel']}] {e['id']} 건너뜀 — 같은 날 이미 게시")
            continue
        if now - due > LATE_LIMIT:
            res[e["id"]] = {"status": "missed", "at": stamp, "channel": e["channel"], "due": e["due"]}
            changed = True
            print(f"[{e['channel']}] {e['id']} 놓침 — 예정 {e['due']}, 3시간 초과 (PC가 다시 잡음)")
            continue
        print(f"[{e['channel']}] {e['id']} {e['kind']} 게시 대상 (예정 {e['due']})" + (" — 미리보기" if dry else ""))
        if dry:
            continue
        token = "".join(ch for ch in os.environ.get(e["token_env"], "") if ch.isprintable() and not ch.isspace())
        try:
            if not token:
                raise IGError(f"저장소 Secret {e['token_env']} 없음")
            mid, link, note = post(e, token)
            res[e["id"]] = {"status": "published", "at": stamp, "channel": e["channel"], "due": e["due"],
                            "permalink": link, "media_id": mid, "note": note}
            print(f"  게시 완료 → {link} {note}")
        except Exception as ex:  # 다음 실행에서 다시 시도
            n = r.get("attempts", 0) + 1
            res[e["id"]] = {"status": "failed", "at": stamp, "channel": e["channel"], "due": e["due"],
                            "attempts": n, "error": str(ex)[:300]}
            print(f"  ✗ 실패({n}/{MAX_ATTEMPTS}): {str(ex)[:300]}")
        changed = True
    if changed and not dry:
        RESULTS.write_text(json.dumps(res, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    if not changed:
        print("게시할 일정 없음")


if __name__ == "__main__":
    main()
