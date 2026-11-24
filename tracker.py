#!/usr/bin/env python3
"""Track Claude plan usage across several accounts, so you know when to switch.

Claude Code holds one account's login at a time (~/.claude/.credentials.json,
the account itself in ~/.claude.json). Each poll asks Claude's usage endpoint
how far through its 5-hour and weekly limits the logged-in account is (the
same numbers /usage shows; asking uses none of the limit) and appends that to
a log per account. Accounts not logged in keep their last reading, read as 0
once its reset time has passed. Nothing is refreshed or written in ~/.claude:
an expired token just falls back to the reading Claude Code cached itself.

    tracker.py poll      take a reading, write the dashboard, notify if needed
    tracker.py status    one line for the terminal or Claude Code's status line
    tracker.py open      open the dashboard in the browser
    tracker.py name <account> <nickname>   rename an account on the dashboard
    tracker.py switch [account|next|best]  switch Claude Code to another saved login
    tracker.py auto [on|off]   switch by itself when the logged-in account runs out

Logins are saved in ~/.local/state/claude-usage/logins/ (private, 0600) the
first time the tracker sees each account logged in; /login to each once.

State lives in ~/.local/state/claude-usage/ (log.jsonl, accounts.json,
notified.json, dashboard.html).
"""
import json
import os
import subprocess
import sys
import time
import urllib.request
from datetime import datetime, timezone

HOME = os.path.expanduser('~')
STATE = os.path.join(HOME, '.local/state/claude-usage')
LOG = os.path.join(STATE, 'log.jsonl')
ACCOUNTS = os.path.join(STATE, 'accounts.json')
NOTIFIED = os.path.join(STATE, 'notified.json')
PAGE = os.path.join(STATE, 'dashboard.html')
HERE = os.path.dirname(os.path.abspath(__file__))

USAGE_URL = 'https://api.anthropic.com/api/oauth/usage'
# Percent of a limit at which to warn, and how high a limit may be on the
# account suggested instead.
WARN_AT = (95, 100)
WEEK_WARN_AT = (95, 100)
# Percent of the 5-hour limit from which the status line suggests switching.
HINT_AT = 80
FREE_ENOUGH = 70
# Weekly percent one full 5-hour window uses, until the log has enough to
# measure it per account (Pro measured about 0.14 on 2026-11-23).
DEFAULT_WEEK_PER_FIVE = 0.14
# Stay on the logged-in account unless another needs using this much more
# urgently, so it doesn't flip back and forth.
STICKY = 1.5
# How far back the burn rate is measured for the projection, in seconds.
RATE_WINDOW_5H = 45 * 60
RATE_WINDOW_WEEK = 12 * 3600


def read_json(path, default):
    try:
        with open(path) as f:
            return json.load(f)
    except (OSError, ValueError):
        return default


def write_json(path, data):
    tmp = path + '.tmp'
    with open(tmp, 'w') as f:
        json.dump(data, f, indent=1)
    os.replace(tmp, path)


def parse_time(s):
    if not s:
        return None
    return datetime.fromisoformat(s).timestamp()


CREDS = os.path.join(HOME, '.claude/.credentials.json')
CONFIG = os.path.join(HOME, '.claude.json')
LOGINS = os.path.join(STATE, 'logins')
SWITCHED = os.path.join(STATE, 'switched.json')
SETTINGS = os.path.join(STATE, 'settings.json')
# After a switch, a session still running on the old login may write its
# refreshed token back over the new one; for this long such a write is taken
# as that and the switch put back.
HOLD_SWITCH = 10 * 60


def settings():
    return {'autoSwitch': True, **read_json(SETTINGS, {})}


def current_account():
    """The logged-in account (as the credentials say, which is what requests
    go out as) and Claude Code's own cached usage reading."""
    cfg = read_json(CONFIG, {})
    creds = read_json(CREDS, {}).get('claudeAiOauth') or {}
    acct = dict(cfg.get('oauthAccount') or {})
    uuid = creds.get('accountUuid') or acct.get('accountUuid')
    if acct.get('accountUuid') != uuid:
        acct = {'accountUuid': uuid}
    return acct, cfg.get('cachedUsageUtilization') or {}


def write_private(path, data):
    os.makedirs(os.path.dirname(path), mode=0o700, exist_ok=True)
    tmp = path + '.tmp'
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, 'w') as f:
        json.dump(data, f, indent=1)
    os.replace(tmp, path)


def keep_login():
    """Save the logged-in account's login, so it can be switched back to.
    Called on every poll and before every switch, so the copy is the newest
    (Claude Code replaces the refresh token as it refreshes)."""
    whole = read_json(CREDS, {})
    creds = whole.get('claudeAiOauth') or {}
    uuid = creds.get('accountUuid')
    if not uuid or not creds.get('refreshToken'):
        return None
    path = os.path.join(LOGINS, uuid + '.json')
    old = read_json(path, {})
    acct = read_json(CONFIG, {}).get('oauthAccount') or {}
    if acct.get('accountUuid') != uuid:
        acct = old.get('oauthAccount') or {}
    if old.get('credentials') != whole or (acct and old.get('oauthAccount') != acct):
        write_private(path, {'credentials': whole, 'oauthAccount': acct, 'savedAt': time.time()})
    return uuid


def logins():
    out = {}
    if os.path.isdir(LOGINS):
        for f in os.listdir(LOGINS):
            if f.endswith('.json'):
                d = read_json(os.path.join(LOGINS, f), None)
                if d:
                    out[f[:-5]] = d
    return out


def ask_usage(creds):
    """Usage for a login, if its access token is still good. Never refreshes
    it: that would replace the refresh token Claude Code holds."""
    token = creds.get('accessToken')
    if not token or creds.get('expiresAt', 0) <= time.time() * 1000 + 60000:
        return None
    req = urllib.request.Request(USAGE_URL, headers={
        'Authorization': 'Bearer ' + token,
        'anthropic-beta': 'oauth-2025-04-20',
        'User-Agent': 'claude-usage-tracker',
    })
    try:
        with urllib.request.urlopen(req, timeout=15) as r:
            return json.load(r)
    except Exception as e:
        print('usage endpoint failed:', e, file=sys.stderr)
        return None


def limit_of(usage, key):
    w = (usage or {}).get(key) or {}
    if w.get('utilization') is None:
        return None, None
    return float(w['utilization']), w.get('resets_at')


def record(uuid, usage, source, acct, creds):
    accounts = read_json(ACCOUNTS, {})
    if uuid not in accounts:
        email = acct.get('emailAddress') or uuid[:8]
        accounts[uuid] = {'name': email.split('@')[0]}
    plan = creds.get('subscriptionType') or acct.get('organizationType')
    if plan:
        accounts[uuid]['plan'] = plan
    accounts[uuid]['lastSeen'] = time.time()
    write_json(ACCOUNTS, accounts)
    five, five_reset = limit_of(usage, 'five_hour')
    week, week_reset = limit_of(usage, 'seven_day')
    row = {'t': round(time.time()), 'account': uuid, 'source': source,
           'five': five, 'fiveReset': five_reset, 'week': week, 'weekReset': week_reset}
    for key, name in (('seven_day_opus', 'weekOpus'), ('seven_day_sonnet', 'weekSonnet')):
        u, _ = limit_of(usage, key)
        if u is not None:
            row[name] = u
    with open(LOG, 'a') as f:
        f.write(json.dumps(row) + '\n')


def hold_switch():
    """Put a switch back if an old session wrote its own login over it."""
    sw = read_json(SWITCHED, {})
    if not sw or time.time() - sw.get('at', 0) > HOLD_SWITCH:
        return
    creds = read_json(CREDS, {}).get('claudeAiOauth') or {}
    now_on = creds.get('accountUuid')
    if now_on and now_on != sw['to'] and now_on in logins():
        print('a session wrote its old login back; switching again', file=sys.stderr)
        switch_to(sw['to'], quiet=True)


def poll():
    os.makedirs(STATE, exist_ok=True)
    hold_switch()
    active = keep_login()
    acct, cached = current_account()
    creds = read_json(CREDS, {}).get('claudeAiOauth') or {}
    uuid = active or acct.get('accountUuid')
    if uuid:
        usage = ask_usage(creds)
        if usage is not None:
            record(uuid, usage, 'live', acct, creds)
        elif cached.get('accountUuid') == uuid and cached.get('utilization'):
            record(uuid, cached['utilization'], 'cached', acct, creds)
    # Accounts not logged in, while their saved access token lasts (a few
    # hours after switching away).
    for other, saved in logins().items():
        if other == uuid:
            continue
        c = saved['credentials'].get('claudeAiOauth') or {}
        usage = ask_usage(c)
        if usage is not None:
            record(other, usage, 'saved login', saved.get('oauthAccount') or {}, c)
    summary = summarise()
    if not auto_switch(summary):
        notify(summary)
    write_page(summary)


def switch_to(uuid, quiet=False):
    """Make uuid the logged-in account for Claude Code: its saved login into
    the credentials file (running sessions read it again when it changes) and
    its account into ~/.claude.json."""
    keep_login()
    saved = logins().get(uuid)
    if not saved:
        raise SystemExit('no saved login for that account: /login to it once in Claude Code first')
    write_private(CREDS, saved['credentials'])
    if saved.get('oauthAccount'):
        cfg = read_json(CONFIG, None)
        if cfg is not None:
            cfg['oauthAccount'] = saved['oauthAccount']
            mode = os.stat(CONFIG).st_mode & 0o777
            tmp = CONFIG + '.m3usage.tmp'
            fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, mode)
            with os.fdopen(fd, 'w') as f:
                json.dump(cfg, f, indent=2)
            os.replace(tmp, CONFIG)
    write_json(SWITCHED, {'to': uuid, 'at': time.time()})
    name = read_json(ACCOUNTS, {}).get(uuid, {}).get('name', uuid[:8])
    if not quiet:
        print('switched to', name)
    return name


def find_account(word, summary):
    """The account to switch to, and if none, why not."""
    accounts = read_json(ACCOUNTS, {})
    saved = logins()
    if word not in (None, 'next', 'best'):
        hit = [u for u, i in accounts.items() if u.startswith(word) or i.get('name') == word]
        if len(hit) != 1:
            return None, 'no single account matches; saved logins: ' + ', '.join(
                accounts.get(u, {}).get('name', u[:8]) for u in saved)
        if hit[0] not in saved:
            return None, f"no saved login for {word}: /login to it once in Claude Code first"
        return hit[0], None
    active = next((a for a in summary['accounts'] if a['active']), None)
    others = [a for a in summary['accounts'] if a['uuid'] in saved and not a['active']]
    if not others:
        return None, 'no other saved login: /login to another account once in Claude Code first'
    # best: the account to use now; next (and no word): the best other one.
    choice = summary['best'] if word == 'best' else summary['next']
    if choice and choice in saved and not (active and choice == active['uuid']):
        return choice, None
    pct = lambda a: f"5h {a['five'] or 0:.0f}%, week {a['week'] or 0:.0f}%"
    if word == 'best' and active and usable(active):
        return None, f"already on the best account, {active['name']} ({pct(active)})"
    # Nothing else has room: say when the soonest frees up.
    frees = []
    for a in others:
        at = [t for t, v in ((a['fiveReset'], a['five']), (a['weekReset'], a['week']))
              if t and (v or 0) >= 95]
        frees.append((max(at) if at else 0, a))
    when, soonest = min(frees, key=lambda f: f[0])
    lines = ['every other account is at a limit:'] + [
        f"  {a['name']}: {pct(a)}" + (f", free from {hhmm(t)}" if t else '') for t, a in frees]
    lines.append(f"staying on {active['name']} ({pct(active)})" if active else '')
    return None, '\n'.join(l for l in lines if l)


def auto_switch(summary):
    """Out of usage on the logged-in account: switch to the best other one."""
    if not settings()['autoSwitch']:
        return False
    active = next((a for a in summary['accounts'] if a['active']), None)
    if not active or ((active['five'] or 0) < 99 and (active['week'] or 0) < 99):
        return False
    target = pick([a for a in summary['accounts'] if a['uuid'] in logins()], exclude=active['uuid'])
    if not target:
        return False
    sw = read_json(SWITCHED, {})
    if sw.get('to') == target['uuid'] and time.time() - sw.get('at', 0) < 30 * 60:
        return False
    switch_to(target['uuid'], quiet=True)
    subprocess.run(['notify-send', '-a', 'Claude usage', 'Claude usage',
                    f"{active['name']} ran out, so Claude Code is now on {target['name']} "
                    f"(5h {target['five'] or 0:.0f}%, week {target['week'] or 0:.0f}%)."], check=False)
    return True


def load_log(since=None):
    rows = []
    try:
        with open(LOG) as f:
            for line in f:
                try:
                    r = json.loads(line)
                except ValueError:
                    continue
                if since is None or r['t'] >= since:
                    rows.append(r)
    except OSError:
        pass
    return rows


def projection(rows, value_key, reset_key, window, now):
    """When the latest window's limit would be reached at its recent burn rate."""
    if not rows:
        return None
    last = rows[-1]
    if last.get(value_key) is None:
        return None
    same = [r for r in rows if r.get(reset_key) == last.get(reset_key)
            and r.get(value_key) is not None and r['t'] >= last['t'] - window]
    if len(same) < 2 or same[-1]['t'] - same[0]['t'] < 300:
        return None
    rate = (same[-1][value_key] - same[0][value_key]) / (same[-1]['t'] - same[0]['t'])
    if rate <= 0:
        return None
    at = last['t'] + (100 - last[value_key]) / rate
    reset = parse_time(last.get(reset_key))
    return {'at': at, 'beforeReset': reset is None or at < reset, 'perHour': rate * 3600}


def week_per_five(rows):
    """Weekly percent used per percent of the 5-hour limit, from readings in
    the same windows, and whether it was measured."""
    dw = df = 0.0
    for a, b in zip(rows, rows[1:]):
        if a.get('fiveReset') != b.get('fiveReset') or a.get('weekReset') != b.get('weekReset'):
            continue
        if None in (a.get('five'), b.get('five'), a.get('week'), b.get('week')):
            continue
        if b['five'] > a['five']:
            df += b['five'] - a['five']
            dw += max(0.0, b['week'] - a['week'])
    if df >= 25 and dw > 0:
        return dw / df, True
    return DEFAULT_WEEK_PER_FIVE, False


def week_plan(a, ratio, measured, now):
    """How much of the week is left to use and how pressing that is."""
    room = 100 - (a['week'] or 0)
    hours = (a['weekReset'] - now) / 3600 if a['weekReset'] else None
    per_session = ratio * 100
    a.update({
        'weekRoom': room, 'hoursLeft': hours, 'ratio': ratio, 'ratioMeasured': measured,
        # Full 5-hour sessions it would take to use the rest of the week.
        'sessionsLeft': room / per_session if per_session > 0 else None,
        # 5-hour sessions that fit before the reset, back to back.
        'sessionsFit': hours / 5 if hours else None,
        # Weekly percent an hour to use it all by the reset; a week not
        # started yet (no reset time) has nothing to lose.
        'urgency': room / max(hours, 1) if hours else 0.0,
    })
    return a


def summarise():
    """Each account's standing now, and which to use."""
    now = time.time()
    accounts = read_json(ACCOUNTS, {})
    rows = load_log(now - 15 * 86400)
    acct, _ = current_account()
    active = acct.get('accountUuid')
    saved = logins()
    out = []
    for uuid, info in accounts.items():
        mine = [r for r in rows if r['account'] == uuid]
        if not mine:
            continue
        last = mine[-1]
        five, week = last.get('five'), last.get('week')
        five_reset, week_reset = parse_time(last.get('fiveReset')), parse_time(last.get('weekReset'))
        # A window whose reset has passed starts again from nothing.
        if five_reset and five_reset <= now:
            five, five_reset = 0.0, None
        if week_reset and week_reset <= now:
            week, week_reset = 0.0, None
        ratio, measured = week_per_five(mine)
        out.append(week_plan({
            'uuid': uuid, 'name': info.get('name', uuid[:8]), 'plan': info.get('plan'),
            'saved': uuid in saved,
            'active': uuid == active, 'readAt': last['t'], 'source': last.get('source'),
            'five': five, 'fiveReset': five_reset, 'week': week, 'weekReset': week_reset,
            'fiveProj': projection(mine, 'five', 'fiveReset', RATE_WINDOW_5H, now) if uuid == active else None,
            'weekProj': projection(mine, 'week', 'weekReset', RATE_WINDOW_WEEK, now) if uuid == active else None,
        }, ratio, measured, now))
    nxt = pick(out, exclude=active)
    return {'now': now, 'accounts': out, 'best': best_of(out), 'next': nxt['uuid'] if nxt else None}


def headroom(a):
    return min(100 - (a['five'] or 0), 100 - (a['week'] or 0))


def usable(a):
    return (a['five'] or 0) < 95 and (a['week'] or 0) < 98


def pick(accts, exclude=None):
    """The account to use next so the least weekly allowance goes to waste:
    of those with room now, the one whose unused week is due to reset
    soonest for its size (most weekly percent an hour still to use). A week
    not started yet comes last, as it doesn't start counting until it's used."""
    open_ = [a for a in accts if usable(a) and a['uuid'] != exclude]
    if not open_:
        return None
    return max(open_, key=lambda a: (a['urgency'], -(a['five'] or 0)))


def best_of(accts):
    best = pick(accts)
    if best is None:
        return None
    active = next((a for a in accts if a['active']), None)
    # Keep to the logged-in account unless another is clearly more pressing.
    if active and active is not best and usable(active) and active['urgency'] * STICKY >= best['urgency']:
        return active['uuid']
    return best['uuid']


def hhmm(ts):
    return datetime.fromtimestamp(ts).strftime('%a %H:%M') if ts and ts - time.time() > 20 * 3600 \
        else datetime.fromtimestamp(ts).strftime('%H:%M') if ts else '?'


def notify(summary):
    active = next((a for a in summary['accounts'] if a['active']), None)
    if not active:
        return
    sent = read_json(NOTIFIED, {})
    others = [a for a in summary['accounts'] if not a['active']]
    best = pick(summary['accounts'], exclude=active['uuid'])
    if best is None and others:
        best = max(others, key=headroom)
    # Ping only on the reading that crosses a level, not on every reading
    # past it. The previous reading is this account's last one before now;
    # one from an earlier window (its reset passed since) counts as 0.
    mine = [r for r in load_log(time.time() - 8 * 86400) if r['account'] == active['uuid']]
    before = mine[-2] if len(mine) >= 2 else None
    for label, key, reset_key, levels in (('5-hour', 'five', 'fiveReset', WARN_AT),
                                          ('weekly', 'week', 'weekReset', WEEK_WARN_AT)):
        value = active[key]
        if value is None:
            continue
        was = 0.0
        if before and before.get(key) is not None:
            then_reset = parse_time(before.get(reset_key))
            if not then_reset or then_reset > time.time():
                was = before[key]
        for level in levels:
            if not (was < level <= value):
                continue
            # Once per window too, in case a reading wobbles back across a
            # level; the reset time is rounded, as the endpoint gives it to a
            # different fraction of a second on every reading.
            tag = f"{active['uuid']}:{key}:{round((active[reset_key] or 0) / 600)}:{level}"
            if tag in sent:
                continue
            sent[tag] = time.time()
            msg = f"{active['name']} is at {value:.0f}% of its {label} limit (resets {hhmm(active[reset_key])})."
            if best and headroom(best) > 100 - FREE_ENOUGH:
                msg += f"\nSwitch to {best['name']}: 5h {best['five'] or 0:.0f}%, week {best['week'] or 0:.0f}%."
            elif best:
                msg += f"\nNo account has much room: {best['name']} is the freest (5h {best['five'] or 0:.0f}%, week {best['week'] or 0:.0f}%)."
            if best and best['uuid'] in logins():
                # Wait for the button in the background, so the poll can end.
                subprocess.Popen([sys.executable, os.path.abspath(__file__), 'offer', best['uuid'], msg],
                                 start_new_session=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            else:
                subprocess.run(['notify-send', '-a', 'Claude usage', '-u', 'critical',
                                'Claude usage', msg], check=False)
    # Another account's week is going to waste faster: say so once per its week.
    best = next((a for a in summary['accounts'] if a['uuid'] == summary['best']), None)
    if best and not best['active']:
        tag = f"{best['uuid']}:switch:{round((best['weekReset'] or 0) / 3600)}"
        if tag not in sent:
            sent[tag] = time.time()
            subprocess.run(['notify-send', '-a', 'Claude usage', 'Claude usage',
                            f"Use {best['name']} next: {best['weekRoom']:.0f}% of its week is unused "
                            f"(~{best['sessionsLeft']:.1f} full 5-hour sessions) and it resets {hhmm(best['weekReset'])}."],
                           check=False)
    # Forget old notices.
    cutoff = time.time() - 9 * 86400
    write_json(NOTIFIED, {k: v for k, v in sent.items() if v > cutoff})


def status():
    s = summarise()
    active = next((a for a in s['accounts'] if a['active']), None)
    if not active:
        print('usage: no reading yet')
        return
    text = f"{active['name']} 5h {active['five'] or 0:.0f}% · wk {active['week'] or 0:.0f}%"
    p = active.get('fiveProj')
    if p and p['beforeReset']:
        text += f" · full ~{hhmm(p['at'])}"
    best = next((a for a in s['accounts'] if a['uuid'] == s['best']), None)
    nxt = next((a for a in s['accounts'] if a['uuid'] == s['next']), None)
    if best and not best['active']:
        text += f" → use {best['name']} (week resets {hhmm(best['weekReset'])})"
    elif nxt and headroom(active) < 100 - HINT_AT:
        text += f" → switch to {nxt['name']}"
    print(text)


def write_page(summary=None):
    summary = summary or summarise()
    rows = load_log(time.time() - 15 * 86400)
    names = {a['uuid']: a['name'] for a in summary['accounts']}
    with open(os.path.join(HERE, 'dashboard.html')) as f:
        page = f.read()
    data = json.dumps({'summary': summary, 'rows': rows, 'names': names})
    os.makedirs(STATE, exist_ok=True)
    with open(PAGE + '.tmp', 'w') as f:
        f.write(page.replace('/*DATA*/null', data.replace('</', '<\\/')))
    os.replace(PAGE + '.tmp', PAGE)


def main():
    cmd = sys.argv[1] if len(sys.argv) > 1 else 'poll'
    if cmd == 'poll':
        poll()
    elif cmd == 'status':
        status()
    elif cmd == 'switch':
        target, why = find_account(sys.argv[2] if len(sys.argv) > 2 else None, summarise())
        if not target:
            sys.exit(why)
        switch_to(target)
        write_page()
    elif cmd == 'offer':
        out = subprocess.run(['notify-send', '-a', 'Claude usage', '-w', '-A', 'switch=Switch now',
                              'Claude usage', sys.argv[3]], capture_output=True, text=True).stdout.strip()
        if out == 'switch':
            name = switch_to(sys.argv[2], quiet=True)
            write_page()
            subprocess.run(['notify-send', '-a', 'Claude usage', 'Claude usage', 'Now on ' + name], check=False)
    elif cmd == 'auto':
        st = settings()
        if len(sys.argv) > 2:
            st['autoSwitch'] = sys.argv[2] == 'on'
            write_json(SETTINGS, st)
        print('auto switch when out of usage:', 'on' if st['autoSwitch'] else 'off')
    elif cmd == 'page':
        write_page()
    elif cmd == 'open':
        write_page()
        subprocess.run(['xdg-open', PAGE], check=False)
    elif cmd == 'name' and len(sys.argv) == 4:
        accounts = read_json(ACCOUNTS, {})
        hit = [u for u, i in accounts.items() if u.startswith(sys.argv[2]) or i.get('name') == sys.argv[2]]
        if len(hit) != 1:
            sys.exit('no single account matches; known: ' + ', '.join(i['name'] for i in accounts.values()))
        accounts[hit[0]]['name'] = sys.argv[3]
        write_json(ACCOUNTS, accounts)
        write_page()
    else:
        sys.exit(__doc__)


if __name__ == '__main__':
    main()
