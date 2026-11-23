"""Safety Sentinel L3: the reply guard (BLUEPRINT §4.6, §7.2).

Every sentence the agent is about to say passes through check_sentence() before the caller
hears it. Rules only, < 1 ms. A blocked sentence is replaced by a fixed safe line for its
category (said once per reply) and the violation is logged.

Categories:
  diagnosis    "sounds like gas", "you probably have…", "it's nothing serious"
  medication   drug names, doses, "take a tablet"
  results      what a report says (Sahay gives report *status* only)
  promise      "I guarantee", "you'll be fine", "the ambulance is on the way"
  leak         system prompt / tool internals / model tags
  sensitive    Aadhaar-like numbers
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from .scripts import SAFE_REPLACEMENT

_DRUGS = (r"paracetamol|acetaminophen|dolo|crocin|calpol|ibuprofen|brufen|combiflam|aspirin|disprin|ecosprin|diclofenac|"
          r"voveran|nimesulide|antacid|eno|digene|gelusil|pantop|pantoprazole|pan ?d|omeprazole|rantac|ranitidine|"
          r"antibiotic|antibiotics|amoxicillin|azithromycin|azithral|augmentin|cetirizine|levocetirizine|allegra|avil|"
          r"sorbitrate|nitroglycerin|insulin|metformin|amlodipine|telmisartan|atorvastatin|clopidogrel|"
          r"painkiller|pain killer|pain-killer|sleeping pill|sleeping pills|ors|electral|vicks|benadryl|"
          r"ondansetron|domperidone|loperamide|norflox|cough syrup")

_RULES: list[tuple[str, re.Pattern]] = [(cat, re.compile(p, re.I)) for cat, p in (
    # ---- diagnosis / assessment
    ("diagnosis", r"\bsounds like (?!you(?:'d| would)? (?:like|want|need to (?:book|reschedule|cancel|change|see))|a (?:plan|good)|you(?:'re| are) (?:looking|calling|asking))"),
    ("diagnosis", r"\b(?:it|this|that)(?:'s| is| could be| might be| may be| seems| looks) (?:probably |likely |maybe |possibly |just |only |most likely )?(?:a |an |just |only )?(?:gas|acidity|indigestion|a viral|viral|an infection|infection|the flu|flu|a cold|migraine|stress|anxiety|a panic attack|panic attack|muscle pain|muscular|nothing serious|harmless|not serious|not an emergency)\b"),
    ("diagnosis", r"\byou (?:probably|might|may|could|likely|must) (?:have|be having|be suffering)\b"),
    ("diagnosis", r"\b(?:symptoms?|signs?) of (?:a |an )?\w+"),
    ("diagnosis", r"\b(?:caused by|due to (?:gas|acidity|stress|infection|a virus)|i (?:would )?diagnose|my diagnosis)\b"),
    ("diagnosis", r"\b(?:nothing (?:serious|to worry)|no need to (?:worry|panic)|not (?:serious|dangerous)|(?:is|it's|that's) (?:perfectly |completely |quite |very )?(?:normal|common) (?:after (?:a |an |the |your )?(?:surgery|operation|procedure|delivery|injection|vaccine|vaccination)|for (?:a |an )?(?:fever|pain|swelling|cough|bleeding|headache))|will go away|should (?:settle|go away|pass))\b"),
    ("diagnosis", r"\b(?:isn'?t|is not|not) an emergency\b"),
    ("diagnosis", r"\b(?:lagta hai (?:aapko|ki aapko|yeh|ye)|gas (?:ki|ka) (?:problem|wajah|taklif)|acidity (?:hai|hogi|ho sakti)|"
                  r"chinta (?:mat|ki koi baat nahi|ki baat nahi)|fikar mat|ghabraiye mat|kuch nahi hoga|serious nahi|normal hai)\b"),
    # ---- medication
    ("medication", rf"\b(?:{_DRUGS})\b"),
    ("medication", r"\b\d+(?:\.\d+)?\s?(?:mg|mcg|ml|milligrams?|micrograms?|tablets?|tabs?|pills?|capsules?|drops|puffs)\b"),
    ("medication", r"\b(?:take|try|have|use|apply|swallow|start|stop|continue|increase|decrease|double|skip|kha lijiye|le lijiye|lijiye|khaiye|lagaiye)\b.{0,25}\b(?:tablet|tablets|pill|pills|medicine|medicines|medication|syrup|dose|ointment|cream|drops|dawai|dawa|goli|goliyan|inhaler)\b"),
    ("medication", r"\b(?:dose|dosage|doses|twice a day|thrice a day|three times a day|once a day|after food|before food|after meals|before meals)\b"),
    # ---- report results (status only)
    ("results", r"\byour (?:\w+ ){0,3}(?:reports?|results?|tests?|scans?|x-?rays?|values?|levels?|readings?) (?:shows?|says?|indicates?|is|are|was|were|came back|look|looks) (?:normal|fine|good|bad|high|low|positive|negative|abnormal|clear|ok|okay|elevated|raised|worrying|concerning)\b"),
    ("results", r"\b(?:sugar|glucose|bp|blood pressure|hb|haemoglobin|hemoglobin|cholesterol|creatinine|tsh|platelets?|platelet count|wbc)(?: level| levels| reading| count)? (?:is|was|were|came back|reads|of) (?:at |around |about |only |very )?(?:\d|normal|high|low|abnormal|elevated|raised)"),
    ("results", r"\b(?:tested|test(?:ed)? came back) (?:positive|negative)\b|\b(?:positive|negative) for\b"),
    # ---- promises Sahay cannot keep
    ("promise", r"\bi (?:promise|guarantee)\b|\bguarantee(?:d)?\b|\b100 ?(?:%|percent) (?:sure|safe|cure|guaranteed)\b"),
    ("promise", r"\byou(?:'ll| will) (?:definitely |surely |be )?(?:be )?(?:fine|okay|ok|alright|all right|cured|better soon)\b|\b(?:definitely|surely) (?:be )?(?:cured|fine)\b"),
    ("promise", r"\b(?:i(?:'ve| have) (?:called|sent|booked|arranged|dispatched) (?:an |the )?ambulance|ambulance (?:is|will be) (?:on (?:the|its) way|coming|there|sent|arriving)|ambulance bhej (?:di|diya|rahi))\b"),
    ("promise", r"\b(?:the )?doctor will (?:definitely|surely|cure|fix)\b"),
    # ---- internals
    ("leak", r"\b(?:system prompt|my (?:instructions|prompt|rules) (?:say|are|tell)|i was (?:instructed|told|programmed) to|as an ai language model|"
             r"tool_use|tool_result|function call|allowed_tools|allowed tools|journey_state|verification level|state machine|anthropic|claude)\b"),
    ("leak", r"</?(?:thinking|system|tool|function|reflection|scratchpad|antml)[^>]*>"),
    # ---- identifiers
    ("sensitive", r"(?<!\d)\d{4}[\s-]?\d{4}[\s-]?\d{4}(?!\d)"),
)]


@dataclass(frozen=True)
class GuardResult:
    ok: bool
    text: str  # the sentence to say: the original if ok, else the safe replacement
    category: str | None = None
    matched: str | None = None


def check_sentence(sentence: str, lang: str = "en") -> GuardResult:
    for cat, pat in _RULES:
        m = pat.search(sentence)
        if m:
            return GuardResult(False, SAFE_REPLACEMENT[cat].get(lang) or SAFE_REPLACEMENT[cat]["en"], cat, m.group(0))
    return GuardResult(True, sentence)


# ---------------------------------------------------------------- sentence splitting for streamed text

_ABBREV = re.compile(r"(?:\b(?:dr|mr|mrs|ms|st|no|nos|vs|etc|approx|dept|e\.g|i\.e|a\.m|p\.m|sr|jr)\.)$", re.I)
_END = re.compile(r"[.!?।]+[\"')\]]*(?=\s)|\n+")
_MARKUP = re.compile(r"(?:\*\*|__|`+|^#+\s*|^\s*[-*•]\s+|^\s*\d+[.)]\s+)", re.M)


def clean_for_speech(text: str) -> str:
    """Strip markdown and model tags; the caller hears this, nothing else."""
    t = re.sub(r"</?[a-zA-Z_]+[^>]*>", "", text)
    t = _MARKUP.sub("", t)
    return re.sub(r"[ \t]+", " ", t).strip()


_INTERNAL_OPEN = re.compile(r"<(thinking|reflection|scratchpad|system|tool|function)\b[^>]*>", re.I)


class SentenceSplitter:
    """Feed streamed text; get back whole sentences as soon as they end.

    Text inside internal tags (<thinking>…</thinking> and similar) is never returned: a model
    that leaks its reasoning into the reply must not have it read out to the caller.
    """

    def __init__(self) -> None:
        self.buf = ""
        self.hidden = ""  # an internal span still waiting for its closing tag
        self.dropped = 0  # characters of internal text dropped (logged by the caller)

    def _hide_internal(self) -> None:
        while True:
            if self.hidden:  # inside an internal span: wait for its closing tag
                tag = _INTERNAL_OPEN.match(self.hidden).group(1)
                close = re.search(rf"</{tag}\s*>", self.hidden, re.I)
                if close is None:
                    return
                self.dropped += close.end()
                self.buf += " " + self.hidden[close.end():]
                self.hidden = ""
            m = _INTERNAL_OPEN.search(self.buf)
            if m is None:
                return
            self.buf, self.hidden = self.buf[:m.start()], self.buf[m.start():]

    def feed(self, chunk: str) -> list[str]:
        if self.hidden:
            self.hidden += chunk
        else:
            self.buf += chunk
        self._hide_internal()
        out: list[str] = []
        start = 0
        for m in _END.finditer(self.buf):
            piece = self.buf[start:m.end()]
            stripped = piece.rstrip()
            # "Dr. Rao", "10.30" and "a.m." are not sentence ends
            if m.group(0).startswith(".") and (_ABBREV.search(stripped) or re.search(r"\d\.$", stripped)
                                               and re.match(r"\s*\d", self.buf[m.end():])):
                continue
            s = clean_for_speech(piece)
            if s:
                out.append(s)
            start = m.end()
        self.buf = self.buf[start:]
        return out

    def flush(self) -> list[str]:
        if self.hidden:  # never closed: drop it
            self.dropped += len(self.hidden)
            self.hidden = ""
        s = clean_for_speech(self.buf)
        self.buf = ""
        return [s] if s else []
