#!/usr/bin/env python3
"""Peyk unit-economics model.

Token counts are measured from the repository (see docs/cost/README.md for provenance).
Prices are PARAMETERS: every vendor rate below is a stated assumption, not a verified
quote. Update PRICES and re-run; every table in the report regenerates.

    python3 docs/cost/model.py            # human-readable tables
    python3 docs/cost/model.py --latex    # LaTeX table bodies for docs/cost/report.tex
"""
from __future__ import annotations
import pathlib
import sys
from dataclasses import dataclass

# ---------------------------------------------------------------- token profile
# Measured 2026-09-15 from agent/ and workers/. Char counts / 4.
TRIAGE_SYS, TRIAGE_SCHEMA, TRIAGE_BODY, TRIAGE_CTX = 1140, 200, 375, 25
TRIAGE_OUT = 150
ACK_IN, ACK_OUT = 1330, 60
CHAT_SYS, CHAT_TOOLS = 3126, 3350          # CHAT_SYSTEM_PROMPT; 23 @tool schemas
CHAT_HIST, CHAT_OBS, CHAT_MEM, CHAT_MISC = 625, 5000, 150, 150
CHAT_OUT = 250
ROUNDS = 1.4                               # MAX_ROUNDS=2, MAX_DOC_ROUNDS=4
DAYS = 30

TRIAGE_IN = TRIAGE_SYS + TRIAGE_SCHEMA + TRIAGE_BODY + TRIAGE_CTX      # 1740
CHAT_IN = CHAT_SYS + CHAT_TOOLS + CHAT_HIST + CHAT_OBS + CHAT_MEM + CHAT_MISC  # 12401

# ---------------------------------------------------------------- prices ($/MTok)
# ASSUMPTIONS. Anthropic rows are list rates. Open-weight rows are order-of-magnitude
# figures for MID-2026 serverless inference and MUST be re-checked before any decision;
# the network policy in the build environment blocked every pricing source.
@dataclass(frozen=True)
class Price:
    name: str; inp: float; out: float; note: str = ""
    def cost(self, t_in, t_out, cached=0):      # cached = tokens served from cache
        return (t_in - cached)/1e6*self.inp + cached/1e6*self.inp*0.10 + t_out/1e6*self.out

PRICES = {
    "haiku45":  Price("Claude Haiku 4.5",   1.00,  5.00, "liste fiyatı"),
    "sonnet46": Price("Claude Sonnet 4.6",  3.00, 15.00, "liste fiyatı"),
    "sonnet5":  Price("Claude Sonnet 5",    2.00, 10.00, "liste fiyatı"),
    "nano":     Price("Bant A: $\\sim$\\$0,05",     0.05,  0.20, "Nova Micro sınıfı"),
    "micro":    Price("Bant B: $\\sim$\\$0,15",     0.15,  0.30, "Llama 3.2 3B / gpt-oss-20b sınıfı"),
    "small":    Price("Bant C: $\\sim$\\$0,35",     0.35,  0.60, "Llama 3.1 8B / Mistral sınıfı"),
    "mid":      Price("Bant D: $\\sim$\\$0,70",     0.70,  0.90, "Llama 3.3 70B sınıfı"),
}
# Minimum cacheable prefix, tokens. Below this, cache_control silently no-ops.
CACHE_MIN = {"haiku45": 4096, "sonnet46": 1024, "sonnet5": 1024}

PROFILES = {"Hafif": (20, 3), "Orta": (50, 8), "Yogun": (120, 20)}

# Quality-risk parameters (section 10). All three are assumptions the founder should set.
PRICE_POINT, LIFETIME_M, CHURN_GIVEN_MISS = 25.0, 12, 0.5
LTV = PRICE_POINT * LIFETIME_M

# ---------------------------------------------------------------- unit costs
def triage_cost(pk, cached=False, body=TRIAGE_BODY):
    p = PRICES[pk]
    prefix = TRIAGE_SYS + TRIAGE_SCHEMA
    t_in = prefix + body + TRIAGE_CTX
    if cached and prefix >= CACHE_MIN.get(pk, 1024):
        return p.cost(t_in, TRIAGE_OUT, cached=prefix)
    return p.cost(t_in, TRIAGE_OUT)

def chat_cost(pk, ack_pk="haiku45", cached=False, obs=CHAT_OBS, tools=CHAT_TOOLS, rounds=ROUNDS):
    p = PRICES[pk]
    prefix = CHAT_SYS + tools
    var = CHAT_HIST + obs + CHAT_MEM + CHAT_MISC
    if cached and prefix >= CACHE_MIN.get(pk, 1024):
        per = p.cost(prefix + var, CHAT_OUT, cached=prefix)
    else:
        per = p.cost(prefix + var, CHAT_OUT)
    return PRICES[ack_pk].cost(ACK_IN, ACK_OUT) + rounds * per

def monthly(profile, triage_pk="haiku45", chat_pk="sonnet46", **kw):
    mails, turns = PROFILES[profile]
    tc = triage_cost(triage_pk, cached=kw.get("cached", False))
    cc = chat_cost(chat_pk, ack_pk=kw.get("ack_pk", "haiku45"), cached=kw.get("cached", False),
                   obs=kw.get("obs", CHAT_OBS), tools=kw.get("tools", CHAT_TOOLS))
    return mails*DAYS*tc, turns*DAYS*cc, DAYS*cc*0.8      # triage, chat, morning brief

# ---------------------------------------------------------------- scenarios
SCEN = [
    ("S0", "Bugünkü hali", dict()),
    ("S1", "Prompt caching açık", dict(cached=True)),
    ("S2", "S1 + gözlem 50 $\\rightarrow$ 15", dict(cached=True, obs=1500)),
    ("S3", "S2 + araç seti bölünmüş", dict(cached=True, obs=1500, tools=1700)),
    ("S4", "S3 + Sonnet 5", dict(cached=True, obs=1500, tools=1700, chat_pk="sonnet5")),
    ("S5", "S4 + triage Bant C (\$0,35)", dict(cached=True, obs=1500, tools=1700, chat_pk="sonnet5", triage_pk="small")),
    ("S6", "S4 + triage Bant B (\$0,15)", dict(cached=True, obs=1500, tools=1700, chat_pk="sonnet5", triage_pk="micro")),
    ("S7", "S6 + ack de açık model", dict(cached=True, obs=1500, tools=1700, chat_pk="sonnet5", triage_pk="micro", ack_pk="micro")),
    ("S8", "S7 + sohbet de Bant D (riskli)", dict(cached=True, obs=1500, tools=1700, chat_pk="mid", triage_pk="micro", ack_pk="micro")),
]

TABLES = {
 "t1": (r"@{}lrrl@{}", [r"\textbf{Çağrı} & \textbf{Girdi} & \textbf{Çıktı} & \textbf{Sıklık}"]),
 "t2": (r"@{}lrr@{}",  [r"\textbf{Bileşen} & \textbf{Token} & \textbf{Pay}"]),
 "t3": (r"@{}llrrr@{}",[r"& \textbf{Senaryo} & \textbf{Hafif} & \textbf{Orta} & \textbf{Yoğun}",
                        r"& & \multicolumn{3}{c}{\footnotesize \$/kullanıcı/ay}"]),
 "t4": (r"@{}lrrrr@{}",[r"\textbf{Model bandı} & \textbf{\$/MTok} & \textbf{Birim} & \textbf{\$/ay} & \textbf{Tasarruf}"]),
 "t5": (r"@{}lrrlrr@{}",[r"\textbf{Örnek tipi} & \textbf{\$/sa} & \textbf{\$/ay} & \textbf{Sığan model} & \textbf{Başabaş} & \textbf{Kullanıcı}"]),
 "t6": (r"@{}lrr@{}",  [r"\textbf{Model bandı} & \textbf{Tasarruf \$/ay} & \textbf{Başabaş ek kaçırma}"]),
 "t7": (r"@{}lrr@{}",  [r"\textbf{Görev} & \textbf{\$/ay} & \textbf{Pay}"]),
}

def tr(x, nd=2):
    """Turkish number format for the LaTeX tables: dot thousands, comma decimal."""
    t = f"{x:,.{nd}f}"
    return t.replace(",", "\u0001").replace(".", ",").replace("\u0001", ".")

def emit_tabular(tid, body_rows, out_dir):
    """Write a COMPLETE tabular. LaTeX's \input breaks inside an alignment (file hooks
    inject \par), so each fragment must be self-contained."""
    colspec, header = TABLES[tid]
    lines = [r"\begin{tabular}{%s}" % colspec, r"\toprule"]
    lines += [h + r" \\" for h in header]
    lines += [r"\midrule"] + body_rows + [r"\bottomrule", r"\end{tabular}"]
    pathlib.Path(out_dir, tid + ".tex").write_text("\n".join(lines) + "\n")
    return len(body_rows)

def fmt(x): return f"{x:,.2f}"

def main(latex=False, emit=None):
    out = []
    # --- table 1: token profile
    rows1 = [("Triage (Haiku 4.5)", TRIAGE_IN, TRIAGE_OUT, "her dış olay"),
             ("Ack / refleks (Haiku 4.5)", ACK_IN, ACK_OUT, "her sohbet turu"),
             ("Sohbet (Sonnet 4.6)", CHAT_IN, CHAT_OUT, f"her sohbet turu $\\times$ {tr(ROUNDS,1)}")]
    # --- table 2: chat input decomposition
    parts = [("\\code{recent\\_observations} (limit 50)", CHAT_OBS),
             ("23 adet \\code{@tool} şeması", CHAT_TOOLS),
             ("\\code{CHAT\\_SYSTEM\\_PROMPT}", CHAT_SYS),
             ("Geçmiş 10 tur", CHAT_HIST),
             ("Hafıza + kullanıcı + iskele", CHAT_MEM + CHAT_MISC)]
    # --- table 3: scenario matrix
    scen_rows = []
    for sid, label, kw in SCEN:
        cells = []
        for prof in PROFILES:
            t, c, b = monthly(prof, **kw)
            cells.append(t + c + b)
        scen_rows.append((sid, label, cells))
    # --- table 4: triage price sensitivity (orta profile)
    sens = []
    for pk in ("haiku45", "mid", "small", "micro", "nano"):
        u = triage_cost(pk)
        sens.append((PRICES[pk].name, PRICES[pk].inp, PRICES[pk].out, u, u*50*DAYS,
                     (triage_cost("haiku45") - u)*50*DAYS))
    # --- table 5: self-hosting break-even
    gpu = [("g5.xlarge (A10G 24GB)", 1.006, "7-8B, 4-bit"),
           ("g6.2xlarge (L4 24GB)", 0.978, "7-8B"),
           ("g5.12xlarge (4xA10G)", 5.672, "70B, 4-bit")]
    be = []
    saved_per_triage = triage_cost("haiku45")          # vs a self-hosted model at ~0 marginal cost
    for name, hourly, fits in gpu:
        m = hourly * 24 * 30
        triages = m / saved_per_triage
        users = triages / (50 * DAYS)
        be.append((name, hourly, m, fits, triages, users))

    # --- table 6: quality-risk break-even
    risk = []
    base_t = triage_cost("haiku45") * 50 * DAYS
    for pk in ("mid", "small", "micro", "nano"):
        saved = base_t - triage_cost(pk) * 50 * DAYS
        risk.append((PRICES[pk].name, saved, saved / (CHURN_GIVEN_MISS * LTV) * 100))
    # --- cost share by task (orta, S0)
    t, c, b = monthly("Orta")
    tot = t + c + b
    share = [("Triage", t, t/tot*100), ("Sohbet", c, c/tot*100), ("Sabah brief", b, b/tot*100)]

    if not emit and not latex:
        print("== 1. Olculen token profili ==")
        for r in rows1: print(f"  {r[0]:34} {r[1]:>7,} in / {r[2]:>4,} out   {r[3]}")
        print("\n== 2. Sohbet input dagilimi ==")
        for n, v in parts: print(f"  {n.replace(chr(92),''):44} {v:>6,} tok  {v/CHAT_IN*100:5.1f}%")
        print("\n== 3. Senaryo matrisi ($/kullanici/ay) ==")
        print(f"  {'':4} {'':34} " + " ".join(f"{p:>9}" for p in PROFILES))
        for sid, label, cells in scen_rows:
            print(f"  {sid:4} {label:34} " + " ".join(f"{c:>9.2f}" for c in cells))
        print("\n== 4. Triage fiyat duyarliligi (orta: 50 mail/gun) ==")
        for n, i, o, u, m, s in sens:
            print(f"  {n:24} ${i:5.2f}/${o:5.2f}  birim ${u:.5f}  aylik ${m:6.2f}  tasarruf ${s:6.2f}")
        print("\n== 5. Self-host basabas (triage) ==")
        for n, h, m, f, tt, u in be:
            print(f"  {n:24} ${h:5.3f}/sa = ${m:7.0f}/ay  ({f})  basabas {tt:>9,.0f} triage = {u:5.0f} kullanici")
        print("\n== 6. Kalite riski basabas (LTV ${:.0f}, P(churn|miss)={}) ==".format(LTV, CHURN_GIVEN_MISS))
        for n, sv, d in risk:
            print(f"  {n:24} tasarruf ${sv:6.2f}/ay  basabas ek kacirma orani {d:5.2f}%")
        print("\n== 7. Gorev bazinda pay (Orta, S0) ==")
        for n, v, pc in share:
            print(f"  {n:16} ${v:6.2f}  {pc:5.1f}%")
    if emit:
        bodies = {
          "t1": [f"{r[0]} & {tr(r[1],0)} & {tr(r[2],0)} & {r[3]} \\\\" for r in rows1],
          "t2": [f"{n} & {tr(v,0)} & {tr(v/CHAT_IN*100,1)}\\% \\\\" for n, v in parts],
          "t3": [f"\\textbf{{{sid}}} & {label} & " + " & ".join(tr(c) for c in cells) + " \\\\"
                 for sid, label, cells in scen_rows],
          "t4": [f"{n} & {tr(i)} / {tr(o)} & {tr(u,5)} & {tr(m)} & {tr(sv)} \\\\" for n, i, o, u, m, sv in sens],
          "t5": [f"{n} & {tr(h,3)} & {tr(m,0)} & {f} & {tt:,.0f} & {tr(u,0)} \\\\" for n, h, m, f, tt, u in be],
          "t6": [f"{n} & {tr(sv)} & {tr(d)}\\% \\\\" for n, sv, d in risk],
          "t7": [f"{n} & {tr(v)} & {tr(pc,1)}\\% \\\\" for n, v, pc in share],
        }
        for tid, body in bodies.items():
            emit_tabular(tid, body, emit)
            print(f"{emit}/{tid}.tex")

if __name__ == "__main__":
    main(latex="--latex" in sys.argv,
         emit=(sys.argv[sys.argv.index("--emit")+1] if "--emit" in sys.argv else None))
