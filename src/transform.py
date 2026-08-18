# Tour de France / stage-race tracker — serverless Python transform.
#
# Pulls everything off Wikipedia's MediaWiki API + Tissot static assets (no key,
# ToS-allowed, honest UA). It reconstructs the race state AS OF a given moment so
# the same code works mid-race live AND when replaying a finished edition:
#   - current stage  = latest stage whose date <= now (and has a result)
#   - jersey wearers  = that stage's row in the "Classification leadership" table
#   - GC top 5        = "General classification after Stage N" in the per-stage
#                       results sub-article (link derived from the leadership row)
#   - next stage      = current + 1, with its Tissot elevation profile
# For a live race "now" tracks Wikipedia's current state automatically.
#
# Output: a small named-key dict consumed by the four .liquid views.

import re
import unicodedata
from datetime import datetime, timezone, timedelta
from concurrent.futures import ThreadPoolExecutor

API = 'https://en.wikipedia.org/w/api.php'
UA = 'TRMNL Tour de France plugin (https://trmnl.com)'

# The double-brace markers are built at runtime: TRMNL renders the transform
# source through Liquid first, so writing them verbatim would be parsed as tags.
_OO = '{' + '{'
_CC = '}' + '}'

# ===== MODE — the ONLY switch in this plugin (it has NO form fields) ========
# Two modes, nothing else to configure:
#
#   "live"  Production. Tracks the 2026 Tour de France against the real current
#           time. This is what ships to the device — leave MODE = "live".
#
#   "test"  Review / QA. Replays the FINISHED 2025 Tour de France so a reviewer
#           can see every screen without waiting for the 2026 race to happen.
#           Pick the moment to replay with TEST_CLOCK just below.
#
# >>> TO REVIEW: set MODE = "test", then move TEST_CLOCK to any instant of the
#     2025 race to land on a specific screen (times are UTC):
#       '2025-06-20'        pre-race countdown (before stage 1)
#       '2025-07-05T18:00'  racing view (stage 1 done)
#       '2025-07-14T12:00'  a stage in progress (TODAY'S STAGE, not finished yet)
#       '2025-07-14T18:00'  same day after the ~17:30 cut-off -> stage concluded
#       '2025-07-21T12:00'  a rest day (REST DAY card)
#       '2025-07-27T19:00'  after the final stage (race-over / champion screen)
MODE = "live"
TEST_CLOCK = '2025-07-15T18:00'   # only used when MODE == "test"
# ----------------------------------------------------------------------------
# Derived from MODE — no need to edit anything below.
if MODE == "test":
    WIKI_PAGE, MOCK_NOW = '2025 Tour de France', TEST_CLOCK
else:                                       # "live" (production)
    WIKI_PAGE, MOCK_NOW = '2026 Tour de France', None   # None = real UTC now
TISSOT_EVENT = 'tdf'        # Tour de France: rider portraits + elevation profiles
SHOW_PHOTO = True           # rider portraits on/off

# A stage counts as "concluded" once the clock passes this time on its date AND
# Wikipedia has a result for it (live races post results around this hour).
CONCLUDE_HOUR, CONCLUDE_MIN = 17, 30
# A rest day has no stage to "conclude", so the REST DAY card holds until this time
# (plugin clock, ~17:00) on the rest day, then flips to the classic TOMORROW'S STAGE.
REST_FLIP_HOUR, REST_FLIP_MIN = 17, 0
# Elevation profiles auto-scale PER STAGE: the silhouette baseline is the stage's
# LOWEST point and the top is its HIGHEST point — but the range is floored at this
# value so a near-flat stage isn't blown up out of proportion (its bumps still show,
# scaled to ~this many metres). Raise it to flatten gentle stages, lower to exaggerate.
PROFILE_MIN_RANGE_M = 500.0
# ============================================================================

# Tissot timing static host — official rider portraits + stage elevation profiles
# for races Tissot times (Tour de Suisse = 'tds', Tour de France = 'tdf', ...).
# Photo file name = 11-digit UCI ID = "100" + rider-details-id(6) + (base % 97).
TISSOT = 'https://tissottiming.z6.web.core.windows.net'
UCI_RIDERS = 'https://www.uci.org/api/riders/ROA/{year}?pageSize=9999'

LEADERSHIP_NAMES = ('Classification leadership by stage', 'Classification leadership')

TYPE_WORDS = [
    ('Individual time trial', 'Time Trial'), ('Team time trial', 'Team TT'),
    ('Medium mountain', 'Mountain'), ('Mountain', 'Mountain'),
    ('Hilly', 'Hilly'), ('Flat', 'Flat'),
]


def run(input):
    # No form fields — all config comes from MODE at the top of this file.
    page = WIKI_PAGE
    want_photo = SHOW_PHOTO
    event = TISSOT_EVENT
    year = _year_of(page)
    now = _now()

    sections = _sections(page)
    if not sections:
        return _empty(page, 'Could not load article sections.')

    leader_idx = _section_index(sections, LEADERSHIP_NAMES, fuzzy=True)
    route_idx = _section_index(sections, ['Route', 'Route and stages'], fuzzy=False)
    gc_idx = _section_index(sections, ['General classification'])

    # section 0 = the lead/infobox (final classifications + distance/stages/dates).
    texts = _fetch_sections(page, {'leader': leader_idx, 'route': route_idx,
                                   'gc': gc_idx, 'info': 0})
    route_tbl = _route_table(texts.get('route', ''))
    lead = _leadership_by_stage(texts.get('leader', ''))
    infobox = texts.get('info', '')
    stage_total = max(route_tbl) if route_tbl else _infobox_int(infobox, 'stages')

    # A stage is "concluded" once the clock passes ~17:30 on its date AND a result
    # exists. Time-gating (not just date<=now) is what lets MOCK_NOW replay the
    # mid-stage "today's stage not finished yet" state of a historical race day.
    def concluded(n):
        dt = _stage_dt((route_tbl.get(n) or {}).get('date'), year)
        # A team time trial has no individual stage winner — the leadership row
        # carries the winning team ({{UCI team code|...}}), so accept a team too.
        lr = lead.get(n) or {}
        done = lr.get('winner') or lr.get('winner_team') or (route_tbl.get(n) or {}).get('winner')
        return dt is not None and now >= dt and bool(done)

    concluded_ns = [n for n in route_tbl if concluded(n)]
    last = max(concluded_ns) if concluded_ns else 0

    # ---- pre-race: nothing concluded yet -> rich overview + countdown ----
    if last == 0:
        return _pre_race(page, year, now, route_tbl, infobox, event, want_photo)

    race_over = bool(stage_total) and last >= stage_total

    # The card shows TODAY'S in-progress stage until it concludes, then flips to
    # TOMORROW'S (the next upcoming one). Tag is derived from the calendar gap.
    today_n = next((n for n in sorted(route_tbl)
                    if _stage_dayeq(route_tbl[n].get('date'), year, now)), None)
    focus_n = today_n if (today_n and not concluded(today_n)) else last + 1

    cur = lead.get(last, {})
    route_cur = route_tbl.get(last, {})

    # winner badge is reactive to when the last stage actually finished: today, yesterday,
    # or earlier (>= 2 days back, i.e. there was a rest day in between -> "LAST STAGE").
    wd = _stage_date(route_cur.get('date'), year)
    wgap = (now.date() - wd.date()).days if wd else 9
    winner_tag = ("TODAY'S STAGE WINNER" if wgap == 0
                  else "YESTERDAY'S STAGE WINNER" if wgap == 1 else 'LAST STAGE WINNER')

    # GC top-10 + winner time from the per-stage results sub-article (link from the
    # leadership row, e.g. "2025 Tour de France, Stage 1 to Stage 11"). Falls back
    # to the main article's General classification section (live current standings).
    gc_rows, winner_time = _subarticle_stage(cur.get('link') or page, last)
    if not gc_rows:
        gc_rows = _parse_gc(texts.get('gc', ''))

    team_of = {r.get('name'): r.get('team_code', '') for r in gc_rows if r.get('name')}
    uci = _uci_index(year) if (want_photo and event) else {}

    def code_for(name):
        return team_of.get(name) or (uci.get(_norm(name or '')) or {}).get('team', '')

    winner_name = cur.get('winner') or route_cur.get('winner', '')
    # Team time trial: no individual winner — show the winning team instead.
    winner_team_only = cur.get('winner_team', '') if not winner_name else ''
    if winner_team_only:
        winner_name = (uci.get('team:' + winner_team_only) or winner_team_only)
    yellow = cur.get('gc', {})
    points = cur.get('points', {})
    mountain = cur.get('mountain', {})
    youth = cur.get('youth', {})

    # ---- which card to show ----------------------------------------------------
    # Order of the day around a rest day (single rest day = a 2-day gap between the
    # last raced stage and the one that resumes racing):
    #   stage day, pre-conclusion .......... TODAY'S STAGE (live)
    #   stage day, post-conclusion ......... REST DAY (tomorrow) — heads-up card
    #   rest day, before the flip time ..... REST DAY (today)
    #   rest day, after the flip time ...... TOMORROW'S STAGE (racing resumes)
    # All time gating is in the plugin clock, so it works the same on live data
    # (MOCK_NOW unset -> real UTC now) as it does in replay.
    rest_day, rest_when = False, ''
    if race_over:
        focus_n, nxt, stage_tag, profile = last, route_cur, 'FINAL CLASSIFICATION', {}
    elif today_n and not concluded(today_n):
        focus_n = today_n
        nxt, stage_tag = (route_tbl.get(focus_n) or route_cur), "TODAY'S STAGE"
        profile = _stage_profile(event, year, focus_n) if event else {}
    else:
        # between stages: today's stage finished, or today has no stage (rest day).
        focus_n = last + 1
        nxt = route_tbl.get(focus_n) or route_cur
        resume_dt = _stage_date(nxt.get('date'), year)
        last_dt = _stage_date(route_cur.get('date'), year)
        gap_days = (resume_dt.date() - last_dt.date()).days if (resume_dt and last_dt) else 1
        rest_dt = resume_dt - timedelta(days=1) if resume_dt else None   # the rest day before racing resumes
        rest_flip = rest_dt.replace(hour=REST_FLIP_HOUR, minute=REST_FLIP_MIN) if rest_dt else None
        if gap_days >= 2 and rest_flip and now < rest_flip:
            rest_day, stage_tag, profile = True, 'REST DAY', {}
            rest_when = 'TODAY' if now.date() == rest_dt.date() else 'TOMORROW'
        else:
            g = (resume_dt.date() - now.date()).days if resume_dt else 9
            stage_tag = "TODAY'S STAGE" if g == 0 else ("TOMORROW'S STAGE" if g == 1 else 'NEXT STAGE')
            profile = _stage_profile(event, year, focus_n) if event else {}

    # photos: prefer Tissot portrait (by UCI ID), fall back to Wikipedia thumbnail.
    photos, tissot = {}, {}
    if want_photo:
        people = [(winner_name, cur.get('winner_title')), (yellow.get('name'), yellow.get('title')),
                  (points.get('name'), points.get('title')), (mountain.get('name'), mountain.get('title')),
                  (youth.get('name'), youth.get('title'))]
        people += [(r.get('name'), r.get('title')) for r in gc_rows[:10]]
        titles = [t for _, t in people if t]
        photos = _photos(titles)
        if event:
            tissot = _tissot_photos([nm for nm, _ in people], uci, event, year)

    def pic(name, title):
        # Returns (primary, fallback). Tissot is primary when the rider resolves to
        # a UCI id; the Wikipedia thumbnail rides along as the onerror fallback. When
        # there's no UCI id, Wikipedia becomes primary with no further fallback.
        tu = tissot.get(_norm(name or ''), '')
        wu = photos.get(title, '')
        return (tu, wu) if tu else (wu, '')

    def jrow(j):
        nm = j.get('name', '')
        p, a = pic(nm, j.get('title'))
        return {'name': nm, 'code': code_for(nm), 'photo': p, 'alt': a}

    y, g, k, w = jrow(yellow), jrow(points), jrow(mountain), jrow(youth)
    winner_photo, winner_alt = pic(winner_name, cur.get('winner_title'))

    # Race-over headline stats: total distance (cheap, from the infobox) and total
    # elevation gain (sum of every stage's climbing). The elevation sum fetches all
    # stage profiles, so it's only done once the race is over; failures degrade to ''.
    ov_dist = _infobox_distance(infobox)
    total_climb = ''
    if race_over and event and stage_total:
        try:
            with ThreadPoolExecutor(max_workers=8) as _ex:
                _gains = _ex.map(lambda n: _stage_profile(event, year, n).get('gain', 0),
                                 range(1, stage_total + 1))
            _tot = sum(gn or 0 for gn in _gains)
            total_climb = format(_tot, ',') if _tot else ''
        except Exception:
            total_climb = ''

    return dict(
        _base(page, stage_total, now),
        pre_race=False, race_over=race_over, rest_day=rest_day, rest_when=rest_when,
        days_to_start=0, stage_tag=stage_tag, winner_tag=winner_tag,
        ov_distance=ov_dist, total_climb=total_climb,
        next_stage_no=focus_n, next_start=nxt.get('start', ''), next_finish=nxt.get('finish', ''),
        next_date=nxt.get('date', ''), next_type=nxt.get('type', ''), next_km=nxt.get('km', ''),
        next_gain=profile.get('gain', 0),
        profile_path=profile.get('path', ''), profile_climbs=profile.get('climbs', []),
        winner_stage_no=last, winner_name=winner_name,
        winner_team_code=(winner_team_only or code_for(winner_name)
                          or _first_team(route_cur.get('winner_raw', '')) or ''),
        winner_time=winner_time, winner_photo=winner_photo, winner_photo_alt=winner_alt,
        yellow_name=y['name'], yellow_team_code=y['code'], yellow_photo=y['photo'], yellow_photo_alt=y['alt'],
        green_name=g['name'], green_team_code=g['code'], green_photo=g['photo'], green_photo_alt=g['alt'],
        kom_name=k['name'], kom_team_code=k['code'], kom_photo=k['photo'], kom_photo_alt=k['alt'],
        white_name=w['name'], white_team_code=w['code'], white_photo=w['photo'], white_photo_alt=w['alt'],
        gc=[_gc_row(r, *pic(r.get('name'), r.get('title'))) for r in gc_rows[:10]],
    )


# ---------- pre-race overview (countdown + last edition + stage 1) ----------

def _pre_race(page, year, now, route_tbl, infobox, event, want_photo):
    ov_date = _infobox_date(infobox, year)
    start = _range_start_date(ov_date, year) or _stage_date((route_tbl.get(1) or {}).get('date'), year)
    days = max(0, (start.date() - now.date()).days) if start else 0
    stages = (max(route_tbl) if route_tbl else 0) or _infobox_int(infobox, 'stages')

    # last edition's FINAL jersey winners come straight from that article's infobox
    # (first / points / mountains / youth + their team codes) — authoritative.
    prev_year = str(int(year) - 1)
    prev_page = page.replace(year, prev_year) if year in page else (prev_year + ' Tour de France')
    pj = _prev_year_jerseys(prev_page)

    names = [pj[k]['name'] for k in ('gc', 'points', 'mountains', 'youth')]
    titles = [pj[k]['title'] for k in ('gc', 'points', 'mountains', 'youth')]
    photos = _photos([t for t in titles if t]) if want_photo else {}
    uci = _uci_index(prev_year) if (want_photo and event) else {}
    tissot = _tissot_photos(names, uci, event, prev_year) if (want_photo and event) else {}

    def pic(name, title):
        tu = tissot.get(_norm(name or ''), '')
        wu = photos.get(title, '')
        return (tu, wu) if tu else (wu, '')

    def jp(key):
        j = pj[key]
        p, a = pic(j['name'], j['title'])
        return j['name'], j['team'], p, a

    yn, yt, yp, ya = jp('gc')
    gn, gt, gp, ga = jp('points')
    kn, kt, kp, ka = jp('mountains')
    wn, wt, wp, wa = jp('youth')

    s1 = route_tbl.get(1, {})
    profile = _stage_profile(event, year, 1) if (event and s1) else {}

    # NB: header already shows the year, so drop it; abbreviate the month so the
    # narrow DATES cell fits (e.g. "4–26 July 2026" -> "4–26 Jul").
    ov_date_short = re.sub(r'[,\s]+' + str(year) + r'\b', '', ov_date).strip()
    ov_date_short = re.sub(r'(January|February|March|April|May|June|July|August|'
                          r'September|October|November|December)',
                          lambda m: m.group(1)[:3], ov_date_short)

    return dict(
        _base(page, stages, now), pre_race=True, race_over=False, days_to_start=days,
        ov_date=ov_date_short, ov_stages=stages, ov_distance=_infobox_distance(infobox),
        py_year=prev_year,
        yellow_name=yn, yellow_team_code=yt, yellow_photo=yp, yellow_photo_alt=ya,
        green_name=gn, green_team_code=gt, green_photo=gp, green_photo_alt=ga,
        kom_name=kn, kom_team_code=kt, kom_photo=kp, kom_photo_alt=ka,
        white_name=wn, white_team_code=wt, white_photo=wp, white_photo_alt=wa,
        next_stage_no=1, next_start=s1.get('start', ''), next_finish=s1.get('finish', ''),
        next_date=s1.get('date', ''), next_type=s1.get('type', ''), next_km=s1.get('km', ''),
        next_gain=profile.get('gain', 0), profile_path=profile.get('path', ''),
        profile_climbs=profile.get('climbs', []), gc=[])


def _prev_year_jerseys(prev_page):
    box = _section_wikitext(prev_page, 0)
    out = {}
    for key, fld in (('gc', 'first'), ('points', 'points'),
                     ('mountains', 'mountains'), ('youth', 'youth')):
        val = _infobox_field(box, fld)
        team = _infobox_field(box, fld + '_team')
        out[key] = {'name': _first_name(val) or '', 'title': _first_title(val) or '',
                    'team': _first_team(team) or ''}
    return out


def _infobox_field(box, name):
    m = re.search(r'\n\s*\|\s*' + name + r'\s*=\s*([^\n]+)', box or '')
    return m.group(1).strip() if m else ''


def _infobox_int(box, name):
    m = re.search(r'(\d+)', _infobox_field(box, name))
    return int(m.group(1)) if m else 0


def _infobox_distance(box):
    # number only (e.g. "3,302"); the markup labels the cell "TOTAL KM".
    m = re.search(r'([\d][\d.,]*)', _infobox_field(box, 'distance'))
    if not m:
        return ''
    return format(int(round(float(m.group(1).replace(',', '')))), ',')


def _infobox_date(box, year):
    m = (re.search(r'\|\s*date\s*=\s*(\d{1,2}\s*[–-]\s*\d{1,2}\s+[A-Za-z]+\s+' + str(year) + r')', box or '')
         or re.search(r'\|\s*date\s*=\s*([^\n|]*' + str(year) + r')', box or ''))
    return _clean(m.group(1)) if m else ''


def _range_start_date(date_str, year):
    m = re.search(r'(\d{1,2})\s*[–-]\s*\d{1,2}\s+([A-Za-z]+)\s+(\d{4})', date_str or '')
    if m:
        return _stage_date('%s %s %s' % (m.group(1), m.group(2), m.group(3)), year)
    m = re.search(r'(\d{1,2}\s+[A-Za-z]+\s+\d{4})', date_str or '')
    return _stage_date(m.group(1), year) if m else None


def _base(page, stage_total, now):
    return {'race_title': page, 'stage_total': stage_total or 0,
            'updated': int(now.timestamp())}


# ---------- clock ----------

def _now():
    if MOCK_NOW:
        for fmt in ('%Y-%m-%dT%H:%M', '%Y-%m-%d %H:%M', '%Y-%m-%d'):
            try:
                return datetime.strptime(MOCK_NOW, fmt).replace(tzinfo=timezone.utc)
            except ValueError:
                continue
    return datetime.now(tz=timezone.utc)


def _stage_date(date_str, year):
    if not date_str:
        return None
    for fmt in ('%d %B %Y', '%d %B'):
        try:
            d = datetime.strptime(date_str.strip(), fmt)
            if d.year == 1900:
                d = d.replace(year=int(year))
            return d.replace(tzinfo=timezone.utc)
        except ValueError:
            continue
    return None


def _stage_dt(date_str, year):
    # The stage's conclusion instant: its date at the ~17:30 cut-off.
    d = _stage_date(date_str, year)
    return d.replace(hour=CONCLUDE_HOUR, minute=CONCLUDE_MIN) if d else None


def _stage_dayeq(date_str, year, now):
    d = _stage_date(date_str, year)
    return bool(d) and d.date() == now.date()


def _year_of(page):
    m = re.search(r'(20\d{2})', page or '')
    return m.group(1) if m else str(datetime.now().year)


# ---------- Wikipedia fetch ----------

def _get(params):
    try:
        import requests
        params = dict(params, format='json')
        r = requests.get(API, params=params, timeout=4,
                         headers={'User-Agent': UA, 'Accept': 'application/json'})
        return r.json() if r.ok else None
    except Exception:
        return None


def _sections(page):
    data = _get({'action': 'parse', 'page': page, 'prop': 'sections'})
    try:
        return data['parse']['sections']
    except (KeyError, TypeError):
        return []


def _section_index(sections, names, fuzzy=False):
    lower = [n.lower() for n in names]
    for s in sections:
        if (s.get('line') or '').strip().lower() in lower:
            return s.get('index')
    if fuzzy:
        for s in sections:
            line = (s.get('line') or '').lower()
            if any(n in line for n in lower):
                return s.get('index')
    return None


def _fetch_sections(page, wanted):
    valid = {k: v for k, v in wanted.items() if v is not None}  # keep section 0
    if not valid:
        return {}
    items = list(valid.items())
    with ThreadPoolExecutor(max_workers=6) as pool:
        res = list(pool.map(lambda kv: (kv[0], _section_wikitext(page, kv[1])), items))
    return dict(res)


def _section_wikitext(page, idx):
    data = _get({'action': 'parse', 'page': page, 'prop': 'wikitext', 'section': str(idx)})
    try:
        return data['parse']['wikitext']['*']
    except (KeyError, TypeError):
        return ''


def _full_wikitext(page):
    data = _get({'action': 'parse', 'page': page, 'prop': 'wikitext'})
    try:
        return data['parse']['wikitext']['*']
    except (KeyError, TypeError):
        return ''


# ---------- rowspan-aware wikitable grid ----------

def _wikitable_grid(wikitext):
    body = re.split(r'\{\|', wikitext, maxsplit=1)[-1]
    body = re.split(r'\n\|\}', body)[0]
    rows = re.split(r'\n\s*\|-', body)
    grid, pending, ncols = [], {}, 0
    for rwt in rows:
        cells = _row_cells_rs(rwt)
        if not cells and not any(v[0] > 0 for v in pending.values()):
            continue
        if ncols == 0 and cells:
            ncols = len(cells)
        if ncols == 0:
            continue
        row, ci = [None] * ncols, 0
        for col in range(ncols):
            p = pending.get(col)
            if p and p[0] > 0:
                row[col] = p[1]
                p[0] -= 1
            elif ci < len(cells):
                content, rs = cells[ci]
                ci += 1
                row[col] = content
                if rs > 1:
                    pending[col] = [rs - 1, content]
        grid.append(row)
    return grid


def _row_cells_rs(rwt):
    out = []
    for line in rwt.split('\n'):
        s = line.strip()
        if not s or s[0] not in '!|' or s.startswith('|+') or s.startswith('|}') or s.startswith('{|'):
            continue
        sep = '||' if s[0] == '|' else '!!'
        for part in _smart_split(s[1:], sep):
            rs, content = 1, part
            if '|' in part:
                left, right = part.split('|', 1)
                if '=' in left and _OO not in left and '[[' not in left and len(left) < 150:
                    m = re.search(r'rowspan\s*=\s*"?(\d+)', left)
                    if m:
                        rs = int(m.group(1))
                    content = right
            out.append((content.strip(), rs))
    return out


# ---------- leadership table -> per-stage state ----------

def _leadership_by_stage(wikitext):
    out = {}
    if not wikitext:
        return out
    for row in _wikitable_grid(wikitext):
        if len(row) < 6 or not row[0]:
            continue
        n = _stage_no_of(row[0])
        if not n:
            continue
        out[n] = {
            'winner': _first_name(row[1]), 'winner_title': _first_title(row[1]),
            'winner_raw': row[1], 'winner_team': _first_team(row[1]),
            'gc': {'name': _first_name(row[2]), 'title': _first_title(row[2])},
            'points': {'name': _first_name(row[3]), 'title': _first_title(row[3])},
            'mountain': {'name': _first_name(row[4]), 'title': _first_title(row[4])},
            'youth': {'name': _first_name(row[5]), 'title': _first_title(row[5])},
            'link': _stage_link(row[0]),
        }
    return out


def _stage_no_of(cell):
    m = (re.search(r'#Stage\s*(\d+)', cell or '') or re.search(r'\|\s*(\d+)\s*\]\]', cell or '')
         or re.match(r'\s*(\d+)\s*$', (cell or '').strip()))
    return int(m.group(1)) if m else None


def _stage_link(cell):
    # The article holding this stage's results (sub-article or the page itself).
    m = re.search(r'\[\[([^\]|#]+)', cell or '')
    return m.group(1).strip() if m else None


# ---------- per-stage GC + winner time from the results sub-article ----------

def _subarticle_stage(article, n):
    wt = _full_wikitext(article)
    if not wt:
        return [], ''
    m = re.search(r'==+\s*Stage\s*' + str(n) + r'\b\s*==+(.*?)(?:\n==+\s*Stage\s*\d|\Z)', wt, re.S)
    block = m.group(1) if m else wt

    winner_time = ''
    rm = re.search(r'Stage\s*' + str(n) + r'\s*Result(.*?)cyclingresult end', block, re.S | re.I)
    if rm:
        cr = re.search(r'\{\{\s*cyclingresult\s*\|\s*1\s*\|.*', rm.group(1), re.I)
        if cr:
            tm = re.search(r"(?:\d{1,2}\s*h\s*)?\d{1,3}\s*['’]\s*\d{2}\s*[\"”]?", cr.group(0))
            winner_time = _clean(tm.group(0)) if tm else ''

    gm = re.search(r'General classification after Stage\s*' + str(n) + r'(.*?)cyclingresult end',
                   block, re.S | re.I)
    gtext = gm.group(1) if gm else ''
    return _cyclingresult_rows(gtext), winner_time


def _cyclingresult_rows(text):
    # Rows from {{cyclingresult|RANK|[[Rider]]|NAT|{{UCI team code|..}}|time/gap|...}}
    # templates (the format Wikipedia uses for GC / stage result lists).
    rows = []
    for cr in re.finditer(r'\{\{\s*cyclingresult\s*\|\s*(\d+)\s*\|([^\n]*)', text or '', re.I):
        rank, line = cr.group(1), cr.group(0)
        name = _first_name(line)
        if not name:
            continue
        rows.append({'rank': int(rank), 'name': name, 'title': _first_title(line),
                     'team_code': _first_team(line) or '',
                     'time': _first_time(line) or '', 'gap': _first_gap(line) or ''})
        if len(rows) >= 10:
            break
    return rows


def _parse_gc(wikitext):
    if not wikitext:
        return []
    # The main-article GC section may be {{cyclingresult}} templates (current) or a
    # plain wikitable (older format) — try the template form first, then the table.
    rows = _cyclingresult_rows(wikitext)
    if rows:
        return rows
    for row in _table_rows(wikitext):
        name = _first_name(row)
        if not name:
            continue
        rows.append({'rank': len(rows) + 1, 'name': name, 'title': _first_title(row),
                     'team_code': _first_team(row) or '',
                     'time': _first_time(row) or '', 'gap': _first_gap(row) or ''})
        if len(rows) >= 10:
            break
    return rows


def _gc_row(r, photo, alt=''):
    # Packed for Liquid `split: '|||'`: rank|name|team|gap|photo|fallback-photo
    return '|||'.join([str(r.get('rank', '')), r.get('name', ''), r.get('team_code', ''),
                       r.get('gap') or r.get('time', ''), photo or '', alt or ''])


# ---------- Route table (per-stage date / start / finish / km / type / winner) ----------

def _route_table(wikitext):
    out = {}
    if not wikitext:
        return out
    for row in re.split(r'\n\s*\|-', wikitext):
        nm = re.search(r'#Stage\s*(\d+)', row) or re.search(r'^!\s*(\d{1,2})\s*$', row, re.M)
        if not nm:
            continue
        num = int(nm.group(1))
        date_m = re.search(r'\b(\d{1,2}\s+(?:January|February|March|April|May|June|July|'
                           r'August|September|October|November|December))\b', row)
        # The route cell is the "<Start> to <Finish>" line. Cities may be linked
        # ([[Lille]] to Lille) or plain (Toulouse to Toulouse), so don't require [[.
        # The route cell is "<Start> to <Finish>", or a SINGLE city for a circuit /
        # Grand Départ stage (e.g. 2026 "Barcelona (Spain)" -> start == finish).
        # Cities may be linked ([[Lille]]) or plain (Toulouse), so don't require [[.
        start = finish = ''
        for ln in row.splitlines():
            s = ln.strip()
            if not s.startswith('|') or s.startswith('|-') or s.startswith('|}'):
                continue
            if '#Stage' in s or 'File:' in s or 'convert' in s or 'flagathlete' in s.lower():
                continue
            if date_m and date_m.group(1) in s:   # the date cell, not a city
                continue
            body = s.lstrip('|').strip()
            if '|' in body and '=' in body.split('|', 1)[0]:  # drop leading cell style attr
                body = body.split('|', 1)[1].strip()
            if re.search(r'\sto\s', body):
                a, b = re.split(r'\sto\s', body, 1)
                start, finish = _clean_city(a), _clean_city(b)
            elif re.search(r'\[\[', body) and not re.search(r'time trial|mountain|hilly|flat', body, re.I):
                start = finish = _clean_city(body)   # single-city course
            if start and finish:
                break
        km_m = re.search(r'convert\|(\d[\d.]*)\|km', row) or re.search(r'(\d{2,3}(?:\.\d)?)\s*km', row)
        ttype = next((label for word, label in TYPE_WORDS if re.search(word, row, re.I)), '')
        wm = re.search(r'\{\{\s*[Ff]lag\s*athlete[^\n]*', row) or re.search(r'\|\s*(\{\{[Ff]lagathlete[^\n]*)', row)
        row_d = {'date': date_m.group(1) if date_m else '', 'start': start, 'finish': finish,
                 'km': (km_m.group(1) + ' km') if km_m else '', 'type': ttype,
                 'winner': _first_name(wm.group(0)) if wm else '',
                 'winner_raw': wm.group(0) if wm else ''}
        # The same #Stage anchor appears in several tables (data row + nav lists), so
        # MERGE rather than overwrite — a data-less nav chunk can't blank a real row.
        if num in out:
            for k, v in row_d.items():
                if v and not out[num].get(k):
                    out[num][k] = v
        else:
            out[num] = row_d
    return out


def _clean_city(s):
    s = re.sub(r'\[\[(?:[^\]|]+\|)?([^\]]+)\]\]', r'\1', s)
    s = re.sub(r'<ref.*', '', s, flags=re.S)
    s = re.sub(r'\([^)]*\)', '', s)
    s = re.sub(r'[|{}\']', ' ', s)
    return re.sub(r'\s+', ' ', s).strip()


# ---------- name / team / time parsing ----------

FLAG_RE = re.compile(r'\{\{\s*[Ff]lag(?:\s*athlete)?\s*\|\s*(?:\[\[)?([^|\]\}]+?)(?:\]\])?\s*\|', re.I)
FONT_RE = re.compile(r'\{\{\s*font\s*colou?r\s*\|[^|]*\|\s*(?:\[\[)?([^|\]\}]+)', re.I)
LINK_NAME_RE = re.compile(r'\[\[([^\]|]+)(?:\|[^\]]+)?\]\]')
TEAM_RE = re.compile(r'\{\{\s*(?:UCI\s*team\s*code|cycling\s*team\s*code|cteam)\s*\|\s*([A-Z0-9]{2,5})', re.I)
TIME_RE = re.compile(r"\b\d{1,2}h\s*\d{2}[’']\s*\d{2}\"?")

_COUNTRY_HINTS = {'france', 'belgium', 'spain', 'italy', 'netherlands', 'germany', 'denmark',
                  'switzerland', 'slovenia', 'norway', 'united kingdom', 'australia', 'colombia',
                  'ecuador', 'eritrea', 'usa', 'united states', 'general classification',
                  'young rider classification', 'team classification', 'points classification',
                  'mountains classification'}


def _first_name(text):
    m = FLAG_RE.search(text or '')
    if m:
        return _clean_name(m.group(1))
    m = FONT_RE.search(text or '')
    if m:
        return _clean_name(m.group(1))
    for lm in LINK_NAME_RE.finditer(text or ''):
        cand = lm.group(1).strip()
        if not _looks_like_country(cand):
            return _clean_name(cand)
    return None


def _first_title(text):
    m = FLAG_RE.search(text or '')
    if m:
        return _clean(m.group(1).strip())
    m = FONT_RE.search(text or '')
    if m:
        return _clean(m.group(1).strip())
    for lm in LINK_NAME_RE.finditer(text or ''):
        cand = lm.group(1).strip()
        if not _looks_like_country(cand):
            return _clean(cand)
    return None


def _first_team(text):
    m = TEAM_RE.search(text or '')
    return m.group(1).upper() if m else None


def _first_time(text):
    m = TIME_RE.search(text or '')
    if not m:
        m = re.search(r"\b\d{1,2}\s*h\s*\d{2}\s*[’']\s*\d{2}", text or '')
    return _clean(m.group(0)) if m else None


def _first_gap(text):
    m = re.search(r"\+\s*(?:\d{1,2}[’']\s*)?\d{1,2}(?:[’'\"]\s*\d{0,2})?[\"”]?", text or '')
    return _clean(m.group(0)) if m else None


def _looks_like_country(s):
    return s.lower() in _COUNTRY_HINTS


def _clean(s):
    return re.sub(r'\s+', ' ', (s or '').replace('’', "'").replace('”', '"')).strip()


def _clean_name(s):
    s = re.sub(r'^\s*(?:File|Image):.+$', '', s or '')
    return _clean(s.split('(')[0])


def _team_full(code):
    return _TEAM_CODES.get((code or '').upper(), (code or '').upper())


def _team_name_clean(name):
    # UCI rosters give ALL-CAPS names with a "(CODE)" suffix, e.g.
    # "TEAM VISMA | LEASE A BIKE (TVL)" -> "Team Visma – Lease a Bike".
    s = re.sub(r'\s*\([A-Z0-9]{2,4}\)\s*$', '', name or '').strip()
    s = re.sub(r'\s*\|\s*', ' – ', s)
    small = {'a', 'an', 'and', 'the', 'of', 'de', 'p/b'}
    words = s.split()
    out = []
    for i, w in enumerate(words):
        lw = w.lower()
        out.append(lw if (0 < i and lw in small) else (w[:1].upper() + w[1:].lower()))
    return _clean(' '.join(out))


_TEAM_CODES = {}


def _table_rows(wikitext):
    parts = re.split(r'\n\s*\|-\s*\n', wikitext)
    return parts[1:] if len(parts) > 1 else []


def _smart_split(s, sep):
    out, db, dl, buf, i = [], 0, 0, [], 0
    while i < len(s):
        two = s[i:i + 2]
        if two == _OO:
            db += 1; buf.append(two); i += 2; continue
        if two == _CC:
            db = max(0, db - 1); buf.append(two); i += 2; continue
        if two == '[[':
            dl += 1; buf.append(two); i += 2; continue
        if two == ']]':
            dl = max(0, dl - 1); buf.append(two); i += 2; continue
        if db == 0 and dl == 0 and s[i:i + len(sep)] == sep:
            out.append(''.join(buf)); buf = []; i += len(sep); continue
        buf.append(s[i]); i += 1
    out.append(''.join(buf))
    return out


# ---------- Tissot rider portraits (by UCI ID) ----------

def _norm(name):
    s = unicodedata.normalize('NFKD', name or '').encode('ascii', 'ignore').decode().lower()
    return re.sub(r'\s+', ' ', re.sub(r'[^a-z ]', ' ', s)).strip()


def _get_url(url, as_json=True):
    try:
        import requests
        r = requests.get(url, timeout=4, headers={'User-Agent': UA, 'Accept': '*/*'})
        if not r.ok:
            return None
        return r.json() if as_json else r
    except Exception:
        return None


def _uci_index(year):
    # Maps normalized name -> dict with 'rd' (rider-details id) and 'team' code.
    # The roster's teamName carries the code in parentheses, e.g. "LIDL - TREK (LTK)".
    data = _get_url(UCI_RIDERS.format(year=year))
    idx = {}
    try:
        for it in data.get('items', []):
            m = re.search(r'(\d+)', it.get('url', ''))
            if not m:
                continue
            tm = re.search(r'\(([A-Z0-9]{2,4})\)\s*$', it.get('teamName', '') or '')
            rec = {'rd': m.group(1), 'team': tm.group(1) if tm else ''}
            if tm:
                # Also index the team's display name under a 'team:CODE' key (colons
                # never appear in _norm output, so this can't collide with a rider).
                idx.setdefault('team:' + tm.group(1),
                               _team_name_clean(it.get('teamName', '')))
            given, family = _norm(it.get('givenName', '')), _norm(it.get('familyName', ''))
            gtoks, ftoks = given.split(), family.split()
            keys = {given + ' ' + family, family + ' ' + given}
            # Cover compound given AND family names (e.g. Wikipedia "Jonas Vingegaard"
            # vs UCI "Jonas / VINGEGAARD HANSEN"): index every given×family token pair.
            # Also index every contiguous multi-token slice of the family name (e.g.
            # UCI "DEL TORO ROMERO" vs Wikipedia's "del Toro" -- a Spanish/Portuguese
            # double surname without the maternal name), not just single tokens.
            fslices = {family}
            for i in range(len(ftoks)):
                for j in range(i + 1, len(ftoks) + 1):
                    fslices.add(' '.join(ftoks[i:j]))
            for gt in gtoks:
                keys.add(gt + ' ' + family)
                keys.add(family + ' ' + gt)
                for ft in ftoks:
                    keys.add(gt + ' ' + ft)
                    keys.add(ft + ' ' + gt)
                for fs in fslices:
                    keys.add(gt + ' ' + fs)
                    keys.add(fs + ' ' + gt)
            for k in keys:
                if k.strip():
                    idx.setdefault(k.strip(), rec)
    except (AttributeError, TypeError):
        pass
    return idx


def _tissot_pid(rd):
    base = int('100' + str(rd).zfill(6))
    return '%09d%02d' % (base, base % 97)


def _tissot_photos(names, idx, event, year):
    # Pure UCI-id -> URL mapping, NO existence checks. Tissot's coverage of a Grand
    # Tour start list is essentially complete, and HEAD-checking every rider added
    # 15 round-trips that, on a cold serverless container with a 5s budget, would
    # time out and silently drop EVERYONE to Wikipedia. We now emit the Tissot URL
    # deterministically; the markup carries the Wikipedia thumbnail as a per-rider
    # <img onerror> fallback for the rare rider whose Tissot file is missing.
    if not idx:
        return {}
    out = {}
    for name in names:
        key = _norm(name or '')
        if not key or key in out:
            continue
        rd = (idx.get(key) or {}).get('rd')
        if rd:
            out[key] = '%s/riders/team/%s/%s.png' % (TISSOT, year, _tissot_pid(rd))
    return out


def _photos(titles):
    uniq = list(dict.fromkeys([t for t in titles if t]))
    if not uniq:
        return {}
    out = {}
    data = _get({'action': 'query', 'prop': 'pageimages', 'piprop': 'thumbnail',
                 'pithumbsize': 400, 'titles': '|'.join(uniq[:50])})
    try:
        query = data['query']
        norm = {n['to']: n['from'] for n in query.get('normalized', [])}
        for p in query['pages'].values():
            src = (p.get('thumbnail') or {}).get('source')
            if not src:
                continue
            title = p.get('title')
            out[title] = src
            if title in norm:
                out[norm[title]] = src
    except (KeyError, TypeError, AttributeError):
        pass
    return out


# ---------- Tissot stage elevation profile ----------

def _stage_profile(event, year, stage_no):
    url = '%s/events/%s/%s/stageprofile/stage%s.json' % (TISSOT, year, event, stage_no)
    data = _get_url(url)
    try:
        pts = data['points']
    except (KeyError, TypeError):
        return {}
    if not pts:
        return {}
    dmax = (pts[-1]['distance']) or 1
    step = max(1, len(pts) // 80)
    samp = pts[::step]
    if samp and samp[-1] is not pts[-1]:
        samp.append(pts[-1])

    # PER-STAGE metre scale: baseline = the stage's lowest point, top = its highest,
    # but force at least PROFILE_MIN_RANGE_M of range so a near-flat stage isn't blown
    # up out of proportion (its bumps still show, scaled to ~that many metres).
    els = [p['elevation'] for p in pts if isinstance(p.get('elevation'), (int, float))]
    emin = min(els) if els else 0.0
    emax = max(els) if els else 0.0
    erange = max(emax - emin, PROFILE_MIN_RANGE_M)

    def xy(p):
        e = max(emin, min(p['elevation'], emin + erange))
        return (p['distance'] / dmax * 100.0, 38.0 - (e - emin) / erange * 38.0)

    path = ' '.join('%.1f,%.1f' % xy(p) for p in samp)
    climbs = []
    for p in pts:
        typ = p.get('type')
        if typ == 'gpm':
            x, y = xy(p)
            climbs.append('%s|||%.1f|||%.1f|||%s|||%s|||%.1f' % (
                _climb_cat(p), x, y, p.get('title', ''), p.get('elevation', ''),
                p['distance'] / 1000.0))
        elif typ == 'sprint':
            x, y = xy(p)
            # intermediate sprint -> 'S' marker (no climb category / elevation)
            climbs.append('S|||%.1f|||%.1f|||%s|||%s|||%.1f' % (
                x, y, p.get('title', ''), '', p['distance'] / 1000.0))

    # De-cluster overlapping climb markers. The dashed elbow connector still points
    # to each climb's TRUE x (field 1); only the CIRCLE is spread out (appended as
    # field 6) so the big category numbers never overlap. Units are viewBox (0..100).
    if climbs:
        MIN_SP, MARGIN = 8.5, 4.5
        xs = [float(c.split('|||')[1]) for c in climbs]
        order = sorted(range(len(climbs)), key=lambda i: xs[i])
        cx = [0.0] * len(climbs)
        prev = None
        for i in order:
            x = min(max(xs[i], MARGIN), 100.0 - MARGIN)
            if prev is not None and x < prev + MIN_SP:
                x = prev + MIN_SP
            cx[i] = x
            prev = x
        overflow = cx[order[-1]] - (100.0 - MARGIN)
        if overflow > 0:                       # cluster ran off the right edge
            for i in order:
                cx[i] = max(MARGIN, cx[i] - overflow)
        climbs = ['%s|||%.1f' % (c, cx[i]) for i, c in enumerate(climbs)]

    gain = sum(max(0, pts[i]['elevation'] - pts[i - 1]['elevation']) for i in range(1, len(pts)))
    return {'path': path, 'climbs': climbs, 'dist': round(dmax / 1000.0, 1),
            'gain': int(gain)}


def _climb_cat(p):
    m = re.search(r'category-(\w+)', p.get('subtype') or '')
    if not m:
        return ''
    v = m.group(1)
    return 'HC' if v.lower() == 'h' else v.upper()


def _empty(page, msg):
    return {'race_title': page, 'pre_race': True, 'days_to_start': 0, 'stage_total': 0,
            'next_stage_no': 0, 'gc': [], 'error': msg,
            'updated': int(_now().timestamp())}
