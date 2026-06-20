#!/usr/bin/env python3
"""
THE STANDARDS BRIEF  —  your daily tourism-standards intelligence agent
======================================================================
Evolution of tourism_radar.py. Instead of one flat dashboard, it files a
morning brief organised around the three lenses you actually track:

  01  NIDHI & the Ministry  ......  MoT / NIDHI+ / HRACC / classification / SAATHI
  02  The States .............. .... what other states are doing on registration,
                                     classification & compliance of accommodation /
                                     tourism service providers
  03  Global desk ............. .... UN Tourism, GSTC, WTTC, global standards & best practice

It pulls only legitimate public RSS + Google News RSS (no LinkedIn / account scraping),
asks Claude to (a) keep only what matters to YOUR profile, (b) sort each item into a
desk, and (c) write the one-line "Why it's on your desk" tie-back. Then it renders the
HTML brief and (optionally) emails it to you.

USAGE
  python tourism_brief.py --demo        # offline, uses today's bundled sample items -> writes HTML
  python tourism_brief.py               # live: fetch + score with Claude -> writes HTML
  python tourism_brief.py --email       # live + email the brief to MAIL_TO

ENV (only needed for the modes that use them)
  ANTHROPIC_API_KEY   your key                (live scoring)
  SMTP_HOST           e.g. smtp.gmail.com     (email)
  SMTP_PORT           e.g. 587                (email)
  SMTP_USER           your gmail address      (email)
  SMTP_PASS           gmail *app password*    (email)
  MAIL_TO             where to send the brief (email; defaults to SMTP_USER)

Schedule it (see daily-brief.yml for GitHub Actions, or Windows Task Scheduler) and it
lands in your inbox every morning.
"""

import os, sys, json, html, time, datetime, urllib.parse, smtplib, ssl
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText

# ----------------------------------------------------------------------------
# 1. WHO THIS BRIEF IS FOR  — your interest profile (edit freely)
#    Derived from your LinkedIn positioning so Claude scores like you would.
# ----------------------------------------------------------------------------
PROFILE = """
Reader: Rachit Srivastava, Project Manager at NBQP, Quality Council of India (QCI), New Delhi.
Owns NIDHI+ (Ministry of Tourism's national hotel registration & star-classification platform)
and the SAATHI self-certification scheme for budget hospitality. Leads quality outreach
(Gunvatta Yatra) and is building an ESG identity (NSE x Grant Thornton).

He cares about, in rough priority:
  1. NIDHI+ / Ministry of Tourism: classification, re-classification, HRACC, project approvals,
     tourism service provider recognition, OTAs, homestay/B&B/Incredible India guidelines, SAATHI.
  2. STATE-LEVEL standards & compliance: any state introducing or changing registration rules,
     classification, licensing, NOCs, guest-registration, or homestay/B&B policy for accommodation
     units or tourism service providers — especially where it parallels, competes with, or feeds NIDHI+.
  3. GLOBAL best practice: UN Tourism / UNWTO, GSTC, WTTC, hotel classification reform, sustainability
     & ESG certification standards, anything India could benchmark against.
  4. Adjacent: rural/community tourism as livelihood infrastructure, GovTech, quality governance, SDGs.

Down-weight: generic travel/destination marketing, flight/cruise/airline trade news, listicles,
hotel-chain expansion PR, and anything with no policy/standards/compliance angle.
"""

# Tie-back hooks Claude can reference in the "Why it's on your desk" line.
DESK_HOOKS = (
    "NIDHI+ ownership, the work-order value narrative, HRACC/classification operations, "
    "SAATHI self-certification, state-vs-central registration interface, benchmarking other states, "
    "ESG/sustainability certification, MoT reform briefing notes, rural/community tourism livelihood framing."
)

# ----------------------------------------------------------------------------
# 2. SOURCES
# ----------------------------------------------------------------------------
def gnews(query):
    q = urllib.parse.quote(query)
    return f"https://news.google.com/rss/search?q={q}&hl=en-IN&gl=IN&ceid=IN:en"

# Desk 01 — NIDHI & the Ministry
FEEDS_NIDHI = [
    gnews('"NIDHI" tourism hotel classification'),
    gnews('Ministry of Tourism hotel classification HRACC'),
    gnews('"tourism service provider" recognition India Ministry'),
    gnews('Incredible India homestay OR "bed and breakfast" guidelines'),
    gnews('SAATHI tourism hospitality self-certification'),
    gnews('Ministry of Tourism reclassification star hotel India'),
]
# Desk 02 — The States
FEEDS_STATE = [
    gnews('state homestay policy India registration 2026'),
    gnews('tourism registration rules accommodation homestay state'),
    gnews('homestay "bed and breakfast" policy state government India'),
    gnews('mandatory guest registration tourism state India'),
    gnews('tourism department classification accommodation unit state'),
    gnews('UTDB OR "tourism development board" registration homestay'),
]
# Desk 03 — Global
FEEDS_GLOBAL = [
    "https://www.gstc.org/feed/",
    "https://www.untourism.int/rss.xml",
    "https://skift.com/feed/",
    "https://www.phocuswire.com/rss",
    gnews('UN Tourism OR UNWTO hotel classification standard'),
    gnews('GSTC OR WTTC sustainable hotel certification standard'),
    gnews('global hotel classification reform tourism standards'),
]
DESKS = [("NIDHI", FEEDS_NIDHI), ("STATE", FEEDS_STATE), ("GLOBAL", FEEDS_GLOBAL)]

RELEVANCE_FLOOR = 2          # 0..4 ; keep items scored >= this
LOOKBACK_HOURS  = 48         # "daily" with a little overlap so nothing slips through
MAX_PER_DESK    = 6          # keep the brief skimmable
MODEL           = "claude-sonnet-4-6"

# ----------------------------------------------------------------------------
# 3. FETCH
# ----------------------------------------------------------------------------
def fetch(feeds, default_desk):
    import feedparser
    cutoff = datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(hours=LOOKBACK_HOURS)
    out, seen = [], set()
    for url in feeds:
        try:
            d = feedparser.parse(url)
        except Exception as e:
            print(f"  ! feed failed: {url[:60]} ({e})", file=sys.stderr); continue
        for e in d.entries[:25]:
            title = (e.get("title") or "").strip()
            link  = (e.get("link") or "").strip()
            if not title or not link: continue
            key = title.lower()[:90]
            if key in seen: continue
            # recency
            t = e.get("published_parsed") or e.get("updated_parsed")
            when = datetime.datetime(*t[:6], tzinfo=datetime.timezone.utc) if t else None
            if when and when < cutoff: continue
            seen.add(key)
            src = ""
            if e.get("source") and e.source.get("title"): src = e.source.title
            elif d.feed.get("title"): src = d.feed.title
            out.append({
                "title": title, "link": link, "default_desk": default_desk,
                "source": src or "news", "summary": (e.get("summary") or "")[:400],
                "when": when.strftime("%d %b") if when else "recent",
            })
    return out

# ----------------------------------------------------------------------------
# 4. SCORE with Claude  — relevance, desk, and the desk-tie-back line
# ----------------------------------------------------------------------------
def score(items):
    from anthropic import Anthropic
    client = Anthropic()
    payload = [{"i": n, "title": it["title"], "source": it["source"],
                "summary": it["summary"], "suggested_desk": it["default_desk"]}
               for n, it in enumerate(items)]
    prompt = f"""You are the editor of a daily tourism standards brief for this reader:

{PROFILE}

For EACH item below, return JSON only (no prose, no markdown fences) as a list of objects:
  {{"i": <index>,
    "score": <0-4 relevance to the reader; 0=ignore, 4=must-read>,
    "desk": "NIDHI" | "STATE" | "GLOBAL",
    "sowhat": "<=22 words, plain English, what changed / what it says>",
    "desk_line": "<=22 words: why it's on HIS desk specifically, referencing one of: {DESK_HOOKS}>",
    "flag": "high" | "watch" | "none"   # high = act/brief on it; watch = track it
  }}
Use suggested_desk unless the content clearly belongs elsewhere. Be strict on score: a generic
travel story with no standards/compliance/policy angle is 0-1.

ITEMS:
{json.dumps(payload, ensure_ascii=False)}"""
    msg = client.messages.create(model=MODEL, max_tokens=4000,
                                 messages=[{"role": "user", "content": prompt}])
    text = "".join(b.text for b in msg.content if b.type == "text").strip()
    text = text.replace("```json", "").replace("```", "").strip()
    verdicts = {v["i"]: v for v in json.loads(text)}
    kept = []
    for n, it in enumerate(items):
        v = verdicts.get(n)
        if not v or v.get("score", 0) < RELEVANCE_FLOOR: continue
        it.update(desk=v["desk"], score=v["score"], sowhat=v["sowhat"],
                  desk_line=v["desk_line"], flag=v.get("flag", "none"))
        kept.append(it)
    kept.sort(key=lambda x: (-x["score"]))
    return kept

# ----------------------------------------------------------------------------
# 5. RENDER
# ----------------------------------------------------------------------------
def esc(s): return html.escape(s or "", quote=True)

def item_html(it):
    sig_cls = {"high": "sig high", "watch": "sig watch"}.get(it.get("flag"), "sig")
    sig_txt = {"high": "Brief on it", "watch": "Watch"}.get(it.get("flag"), f"Signal {it.get('score','')}")
    return f"""
    <div class="item">
      <div class="meta">
        <span class="src">{esc(it['source'])}</span>
        <span class="when">{esc(it['when'])}</span>
        <span class="{sig_cls}">{esc(sig_txt)}</span>
      </div>
      <h3><a href="{esc(it['link'])}" target="_blank" rel="noopener">{esc(it['title'])}</a></h3>
      <p class="sowhat">{esc(it.get('sowhat',''))}</p>
      <div class="desk"><span class="lbl">Your desk</span><span>{esc(it.get('desk_line',''))}</span></div>
    </div>"""

def render(items, lead, template_path):
    by = {"NIDHI": [], "STATE": [], "GLOBAL": []}
    for it in items: by.get(it["desk"], by["NIDHI"]).append(it)
    for k in by: by[k] = by[k][:MAX_PER_DESK]
    now = datetime.datetime.now()
    def block(lst, label):
        return "".join(item_html(x) for x in lst) if lst else \
            f'<div class="item"><p class="sowhat">No new {label} items cleared the bar today.</p></div>'
    tpl = open(template_path, encoding="utf-8").read()
    n_flag = sum(1 for it in items if it.get("flag") in ("high", "watch"))
    repl = {
        "{{DATE_LONG}}": now.strftime("%A, %d %B %Y"),
        "{{FILED_TIME}}": now.strftime("%H:%M"),
        "{{N_TOTAL}}": str(len(items)),
        "{{N_NIDHI}}": str(len(by["NIDHI"])), "{{N_STATE}}": str(len(by["STATE"])),
        "{{N_GLOBAL}}": str(len(by["GLOBAL"])), "{{N_FLAG}}": str(n_flag),
        "{{LEAD}}": esc(lead),
        "{{SECTION_NIDHI}}": block(by["NIDHI"], "NIDHI/Ministry"),
        "{{SECTION_STATE}}": block(by["STATE"], "state"),
        "{{SECTION_GLOBAL}}": block(by["GLOBAL"], "global"),
    }
    for k, v in repl.items(): tpl = tpl.replace(k, v)
    return tpl

# Seed for the running central-vs-state comparison (Deep Dive desk).
# Add a state row here whenever a new one appears; the agent ships this as the baseline.
SEED_DEEPDIVE = {
  "head": "The centre is going voluntary while the states go mandatory",
  "intro": ("NIDHI+ is a voluntary national scheme — and the Ministry has just narrowed it by ending "
            "project approvals. But several states are moving the opposite way: making registration "
            "compulsory, and in Uttarakhand's case running their own classification. Here's where each stands."),
  "take": ("The risk is NIDHI+ becoming one registry among many. The opportunity is to position it as the "
           "interoperable national spine that state systems plug into — a strong line for your reform note."),
  "states": [
    {"name":"Centre · NIDHI+ (MoT)","vs":"The national scheme","mandatory":False,"ownclass":True,"validity":"Voluntary",
     "note":"Voluntary star classification; project approvals for under-construction units stopped 16 March 2026."},
    {"name":"Uttarakhand","vs":"Parallel regime","mandatory":True,"ownclass":True,"validity":"5-year + renewal",
     "note":"2026 Rules: one mandatory framework for every unit, with homestay and B&B classification built in."},
    {"name":"Rajasthan","vs":"State-run, lighter","mandatory":True,"ownclass":False,"validity":"Single-window",
     "note":"Homestay Scheme 2026: fast digital approval, rooms raised 5 to 8, owner-residence rule dropped."},
    {"name":"Uttar Pradesh","vs":"Layers on NIDHI+","mandatory":True,"ownclass":False,"validity":"Policy + NOC",
     "note":"First dedicated state homestay/B&B policy; local-body NOC now mandatory on top of NIDHI+ listing."},
    {"name":"Meghalaya","vs":"Own data registry","mandatory":True,"ownclass":False,"validity":"App-based",
     "note":"Mandatory guest registration via the state tourism app, driven by visitor-safety concerns."},
  ],
}

def build_summary(items):
    highs = [it for it in items if it.get("flag") == "high"]
    spoken = (f"Here is your brief in twenty seconds. {len(items)} items today across the desks, "
              f"{len(highs)} flagged for action. ")
    if highs:
        spoken += f"The big one: {highs[0]['sowhat']} "
    spoken += "And the thread to watch is the centre versus the states on mandatory registration."
    hls = [it["sowhat"] for it in highs][:3] or [it["sowhat"] for it in items[:3]]
    return {"headline": "Centre vs states: the standards map is shifting",
            "spoken": spoken, "highlights": hls}

def build_deepdive(items):
    """Returns the running central-vs-state comparison. Ships the seeded baseline.
    To auto-extend it from new state items, wire a Claude call here that merges new
    rows into SEED_DEEPDIVE['states']; falling back to the seed keeps it robust."""
    return SEED_DEEPDIVE

def dump_json(items, path):
    """Publish the brief as JSON for the Shivam voice app to read each morning."""
    payload = {
        "date": datetime.datetime.now().strftime("%A, %d %B %Y"),
        "reader": "Rachit",
        "summary": build_summary(items),
        "deepdive": build_deepdive(items),
        "items": [{
            "desk": it["desk"], "source": it["source"], "when": it["when"],
            "flag": it.get("flag", "none"), "title": it["title"],
            "sowhat": it.get("sowhat", ""), "desk_line": it.get("desk_line", ""),
            "link": it["link"], "pdf": it.get("pdf", ""),
        } for it in items],
    }
    with open(path, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)

def pick_lead(items):
    """Lead = highest-priority item, phrased as the one thing to know."""
    flagged = [it for it in items if it.get("flag") == "high"] or items
    if not flagged: return "A quiet morning — nothing cleared the relevance bar in the last 48 hours."
    top = flagged[0]
    return f"{top['title']} — {top.get('sowhat','')}"

# ----------------------------------------------------------------------------
# 6. EMAIL
# ----------------------------------------------------------------------------
def email_brief(html_body, subject):
    host = os.environ["SMTP_HOST"]; port = int(os.environ.get("SMTP_PORT", "587"))
    user = os.environ["SMTP_USER"]; pw = os.environ["SMTP_PASS"]
    to   = os.environ.get("MAIL_TO", user)
    m = MIMEMultipart("alternative")
    m["Subject"] = subject; m["From"] = user; m["To"] = to
    m.attach(MIMEText("Open in an HTML-capable client to read The Standards Brief.", "plain"))
    m.attach(MIMEText(html_body, "html"))
    ctx = ssl.create_default_context()
    with smtplib.SMTP(host, port) as s:
        s.starttls(context=ctx); s.login(user, pw); s.sendmail(user, [to], m.as_string())
    print(f"  -> emailed to {to}")

# ----------------------------------------------------------------------------
# 7. DEMO DATA — today's real items (so --demo shows a true edition offline)
# ----------------------------------------------------------------------------
DEMO = [
 {"default_desk":"NIDHI","desk":"NIDHI","score":4,"flag":"high","source":"Ministry of Tourism","when":"16 Mar",
  "link":"https://nidhi.tourism.gov.in/",
  "title":"MoT discontinues Voluntary Scheme of Project Approvals for under-construction hospitality units",
  "sowhat":"From 16 Mar 2026 no fresh project-approval applications are accepted at construction/pre-construction stage.",
  "desk_line":"Directly changes NIDHI+ intake; a concrete line for your Secretary reform briefing and work-order narrative."},
 {"default_desk":"NIDHI","desk":"NIDHI","score":3,"flag":"watch","source":"Travel Trade Journal","when":"recent",
  "link":"https://traveltradejournal.com/ministry-of-tourism-streamlines-hospitality-approvals-and-classification-via-nidhi-portal/",
  "title":"Ministry tells Lok Sabha NIDHI+ is the single online system for classification & TSP recognition",
  "sowhat":"MoT frames NIDHI+ as the system of record for classifying accommodation units and recognising service providers.",
  "desk_line":"Useful framing language for positioning NIDHI+ value and the unpaid work-order case."},
 {"default_desk":"STATE","desk":"STATE","score":4,"flag":"high","source":"Pioneer Edge","when":"this week",
  "link":"https://pioneeredge.in/new-tourism-registration-rules-in-force-homestays-under-one-framework/",
  "title":"Uttarakhand notifies Tourism & Travel Business Registration Rules 2026 — one framework for all units",
  "sowhat":"Mandatory UTDB registration for hotels, homestays, B&Bs, agents & adventure ops; 5-year validity; homestay classification built in.",
  "desk_line":"A state building a parallel registration + classification regime beside NIDHI+ — the central-vs-state interface is your core domain."},
 {"default_desk":"STATE","desk":"STATE","score":3,"flag":"watch","source":"Business Standard","when":"recent",
  "link":"https://www.business-standard.com/industry/news/rajasthan-unveils-homestay-scheme-2026-to-boost-tourism-126022300883_1.html",
  "title":"Rajasthan Homestay Scheme 2026: single-window digital approval, rooms raised 5→8",
  "sowhat":"Owner-residence requirement dropped; caretaker model allowed; faster digital registration to grow rural homestays.",
  "desk_line":"Light-touch state compliance model + the rural/community-tourism livelihood framing you're positioning around."},
 {"default_desk":"STATE","desk":"STATE","score":3,"flag":"watch","source":"Deccan Herald","when":"recent",
  "link":"https://www.deccanherald.com/india/uttar-pradesh/up-govt-approves-homestay-bed-and-breakfast-policy-to-boost-tourism-3569077",
  "title":"UP approves first dedicated Homestay & B&B Policy; mandates local-body NOCs",
  "sowhat":"Until now UP units registered via NIDHI+; new policy layers local NOCs and state incentives on top.",
  "desk_line":"Shows states layering local compliance over NIDHI+ — relevant to SAATHI self-cert scope."},
 {"default_desk":"STATE","desk":"STATE","score":2,"flag":"watch","source":"NewsOnAir","when":"recent",
  "link":"https://www.newsonair.gov.in/meghalaya-govt-directs-mandatory-visitor-registration-after-murder-incident",
  "title":"Meghalaya makes guest registration via state tourism app compulsory",
  "sowhat":"Safety-driven mandatory digital guest registration across homestays, resorts and landlords.",
  "desk_line":"Compliance-as-safety angle; a state digital guest registry that parallels NIDHI's data role."},
 {"default_desk":"GLOBAL","desk":"GLOBAL","score":3,"flag":"watch","source":"Hotel Management","when":"recent",
  "link":"https://www.hotelmanagement-network.com/news/hotel-sustainability-basics-become-global-industry-benchmark/",
  "title":"WTTC 'Hotel Sustainability Basics' — 12 minimum actions — emerge as a global benchmark",
  "sowhat":"An achievable entry-level floor that corporate buyers, regulators and investors increasingly expect.",
  "desk_line":"A minimum-floor model you could reference for an ESG-linked baseline tier in SAATHI / classification."},
 {"default_desk":"GLOBAL","desk":"GLOBAL","score":3,"flag":"watch","source":"GSTC","when":"recent",
  "link":"https://www.gstc.org/",
  "title":"GSTC positioned as the 'standard for standards'; OTAs start prioritising certified properties",
  "sowhat":"GSTC accredits certifiers rather than certifying hotels directly; Booking.com/Google Travel surface certified stays.",
  "desk_line":"Accreditation-of-certifiers architecture is a model for how QCI/NIDHI+ could position; ties to your ESG track."},
 {"default_desk":"GLOBAL","desk":"GLOBAL","score":2,"flag":"watch","source":"UN Tourism","when":"recent",
  "link":"https://www.untourism.int/technical-cooperation/the-update-of-hotel-classification-scheme",
  "title":"UN Tourism backing national hotel-classification updates: 'business-friendly, future-oriented, guest-centric'",
  "sowhat":"A 3-year programme to redeploy revised, more credible and guest-focused classification schemes.",
  "desk_line":"External validation + benchmark language for any move to modernise India's classification criteria."},
]

# ----------------------------------------------------------------------------
# 8. MAIN
# ----------------------------------------------------------------------------
def main():
    demo  = "--demo" in sys.argv
    email = "--email" in sys.argv
    here  = os.path.dirname(os.path.abspath(__file__))
    template = os.path.join(here, "brief_template.html")
    out = os.path.join(here, "standards_brief.html")

    if demo:
        items = DEMO
    else:
        items = []
        for desk, feeds in DESKS:
            print(f"fetching {desk} ...", file=sys.stderr)
            items += fetch(feeds, desk)
        print(f"  {len(items)} raw items; scoring with Claude ...", file=sys.stderr)
        items = score(items)
        print(f"  {len(items)} cleared the bar.", file=sys.stderr)

    page = render(items, pick_lead(items), template)
    with open(out, "w", encoding="utf-8") as f: f.write(page)
    print(f"wrote {out}")

    brief_json = os.path.join(here, "brief.json")
    dump_json(items, brief_json)
    print(f"wrote {brief_json}  ({len(items)} items for Shivam)")

    if email:
        subj = f"The Standards Brief — {datetime.datetime.now():%a %d %b}"
        email_brief(page, subj)

if __name__ == "__main__":
    main()
