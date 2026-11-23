"""Safety Sentinel L1: deterministic checks on every caller message (BLUEPRINT §4.6).

Fast regexes, no model. They decide things the LLM must never get wrong:

  red_flags(text)        emergency / urgent phrases (English, romanised Hindi, Devanagari, Kannada)
  wants_human(text)      "staff", "insaan se baat karao", "real person"
  declines_recording(t)  "don't record", "record mat karo"
  injection_attempt(t)   "ignore your instructions", "show me your prompt"
  is_clear_yes(text)     an unambiguous yes (needed before booking / cancelling / registering)
  guess_language(text)   en / hi-en / hi / kn, for choosing the scripted replies

Phrase lists are illustrative and need native-speaker and clinical sign-off (BLUEPRINT §4.6).
Precision matters too: "chest X-ray appointment" and "dil ke doctor" must not fire.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

# ---------------------------------------------------------------- normalising

_PUNCT = re.compile(r"[.,!?;:()\[\]{}\"“”*_/\\|~`^+=<>@#$%&।]+")
_SPACE = re.compile(r"\s+")


def normalise(text: str) -> str:
    t = text.lower().replace("’", "'").replace("‘", "'")
    t = _PUNCT.sub(" ", t)
    return _SPACE.sub(" ", t).strip()


# Romanised Hindi has many spellings: one alternation per sound.
IN = r"(?:me|mein|mei|main|mai|mien|men)"
RAHA = r"(?:raha|rha|rahi|rhi|rahe|rhe)"
NAHI = r"(?:nahi|nahin|nai|nhi|nahee)"
CHEST_HI = r"(?:seene|sine|seena|sina|seenay|chhati|chati|chaati|chhaati)"
PAIN_HI = r"(?:dard|dart|dabav|dabaav|dabaw|bhaaripan|bharipan)"
SAANS = r"(?:saans|sans|saas|sas|saans)"
WHO = r"(?:my|his|her|their|the|our|a)"


@dataclass(frozen=True)
class RedFlag:
    id: str
    tier: str  # EMERGENCY or URGENT
    route: str  # EMERGENCY_DESK, TELE_MANAS_AND_HUMAN, TRIAGE_NURSE
    matched: str


@dataclass(frozen=True)
class _Rule:
    id: str
    tier: str
    route: str
    patterns: tuple[re.Pattern, ...]
    negatable: bool = True


def _rule(id_: str, tier: str, patterns: list[str], route: str | None = None, negatable: bool = True) -> _Rule:
    route = route or ("EMERGENCY_DESK" if tier == "EMERGENCY" else "TRIAGE_NURSE")
    compiled = tuple(re.compile(p if not p.isascii() else rf"(?<![a-z']){p}(?![a-z])") for p in patterns)
    return _Rule(id_, tier, route, compiled, negatable)


RULES: tuple[_Rule, ...] = (
    _rule("chest_pain", "EMERGENCY", [
        r"chest (?:pain|pains|pressure|tightness|heaviness)",
        r"(?:pain|pressure|tightness|heaviness) in (?:my |his |her |the )?chest",
        r"(?:tight|heavy|crushing) chest",
        r"heart attack",
        rf"{CHEST_HI} {IN} (?:bahut |tez |halka |thoda |bhi )?{PAIN_HI}",
        rf"{CHEST_HI} (?:dab|daba|dabb) {RAHA}",
        rf"dil {IN} (?:bahut |tez )?{PAIN_HI}",
        rf"(?:dil ka daura|heart attack aa)",
        r"ede ?novu", r"edeyalli novu", r"ede (?:hidid|bhaara)",
        "सीने में दर्द", "छाती में दर्द", "दिल का दौरा", "दिल में दर्द",
        "ಎದೆ ನೋವು", "ಎದೆಯಲ್ಲಿ ನೋವು", "ಹೃದಯಾಘಾತ",
    ]),
    _rule("breathless", "EMERGENCY", [
        r"(?:can'?t|cannot|can not|unable to|not able to|struggling to|hard to) breathe",
        r"(?:difficulty|trouble|problem|problems) (?:in )?breathing",
        r"breathing (?:difficulty|problem|trouble|is difficult|is hard)",
        r"short(?:ness)? of breath", r"breathless(?:ness)?", r"gasping",
        r"not breathing", r"stopped breathing",
        rf"{SAANS} {NAHI} (?:aa|a|le pa|li ja) {RAHA}",
        rf"{SAANS} (?:lene {IN} )?(?:dikkat|diqqat|taklif|takleef|pareshani|problem|mushkil)",
        rf"{SAANS} (?:phool|ful|ruk|atak|chadh|fool) {RAHA}",
        rf"{SAANS} (?:ruk|atak|band ho) (?:gayi|gai|gaya)",
        rf"dam ghut {RAHA}", rf"dum ghut {RAHA}", rf"ghutan ho {RAHA}",
        r"usiru katt", r"usiraata kashta", r"usiru (?:aadtilla|aadta illa|kattide)",
        "साँस नहीं आ रही", "सांस नहीं आ रही", "सांस लेने में तकलीफ", "साँस लेने में दिक्कत", "दम घुट",
        "ಉಸಿರಾಟದ ತೊಂದರೆ", "ಉಸಿರಾಟ ತೊಂದರೆ", "ಉಸಿರು ಕಟ್ಟ",
    ]),
    _rule("stroke", "EMERGENCY", [
        r"face (?:is )?(?:droop|drooping|dropping|twisted|tilted)", r"(?:droop|drooping) (?:face|mouth)",
        r"(?:can'?t|cannot|unable to|not able to) (?:move|lift|feel|use) (?:my |his |her |their )?(?:left |right )?(?:arm|leg|hand|one side|side|face)",
        r"slurred speech", r"slurring", r"speech is slurred", r"can'?t (?:speak|talk) properly",
        r"sudden(?:ly)? (?:weak|numb|weakness|numbness)", r"one side (?:of (?:the |his |her |my )?body )?(?:is )?(?:weak|numb|paralysed|paralyzed)",
        r"(?:having|had|has) a stroke", r"paralysis attack", r"brain attack",
        rf"(?:muh|munh|muhn|mooh) (?:tedha|tehda|terha|tedhi|tehdi)",
        rf"(?:haath|hath|pair|per|aadha sharir) (?:sunn|sun|bekaar) (?:ho|pad) (?:gaya|gya|gayi|gai|raha|rha)",
        rf"(?:haath|hath|pair) kaam {NAHI} kar {RAHA}",
        rf"bolne {IN} (?:dikkat|taklif|takleef|pareshani)", r"(?:zubaan|zuban|jubaan|juban) (?:ladkhada|ladkhad|lad khada)",
        r"lakwa", r"laqwa", r"lakhwa",
        "लकवा", "मुंह टेढ़ा", "मुँह टेढ़ा", "बोलने में दिक्कत",
        "ಪಾರ್ಶ್ವವಾಯು", "ಲಕ್ವ",
    ]),
    _rule("unconscious", "EMERGENCY", [
        r"unconscious", r"unresponsive",
        r"(?:he|she|they|father|mother|dad|mom|papa|mummy|husband|wife|son|daughter|baby|child|grandfather|grandmother|patient|amma|appa) (?:is |'s |isn'?t |is not )?(?:not responding|responding to nothing)", r"(?:won'?t|will not|isn'?t|is not) waking up",
        r"(?:won'?t|will not|can'?t) wake (?:him|her|them) up", r"collapsed",
        rf"behosh", rf"hosh {NAHI}", rf"hosh {IN} {NAHI}", rf"(?:utth|uth) {NAHI} {RAHA}",
        r"prajne (?:illa|tappide)", r"echara illa",
        "बेहोश", "होश नहीं", "ಪ್ರಜ್ಞೆ ಇಲ್ಲ", "ಪ್ರಜ್ಞೆ ತಪ್ಪಿ",
    ]),
    _rule("seizure", "EMERGENCY", [
        r"seizure", r"seizures", r"convulsion", r"convulsions", r"convulsing",
        r"(?:having|had|getting|has) (?:a )?fits?", r"fits (?:aa|aaye|aye|aa rahe|aate)", r"fit (?:aaya|aya|aa gaya|pada)",
        r"mirgi", r"(?:daura|dora) (?:pad|pada|padaa|aaya|aa gaya|aa raha)", rf"jhatke (?:aa|lag) {RAHA}",
        r"(?:fits|pits) bandide", r"apasmara",
        "मिर्गी", "दौरा पड़", "झटके आ रहे", "ಮೂರ್ಛೆ ರೋಗ", "ಫಿಟ್ಸ್",
    ]),
    _rule("severe_bleeding", "EMERGENCY", [
        r"(?:heavy|heavily|a lot of|lots of|severe|profuse|uncontrolled) bleeding", r"bleeding (?:heavily|a lot|badly|profusely)",
        r"(?:won'?t|wont|doesn'?t|does not|isn'?t|is not|not) stop(?:ping)? bleeding",
        r"bleeding (?:won'?t|wont|doesn'?t|does not|isn'?t|is not) stop(?:ping)?",
        r"(?:vomiting|coughing|spitting|throwing up) (?:up )?blood", r"blood in (?:the |his |her |my )?vomit",
        rf"(?:bahut|zyada|jyada|kaafi) (?:khoon|khun) (?:beh|nikal|aa|ja) {RAHA}",
        rf"(?:khoon|khun) (?:ruk|band ho) {NAHI} {RAHA}", rf"(?:khoon|khun) (?:ki|ka) (?:ulti|ultee)",
        r"tumba rakta", r"rakta (?:nilluttilla|hogtide)",
        "बहुत खून", "खून की उल्टी", "खून रुक नहीं", "ತುಂಬಾ ರಕ್ತ", "ರಕ್ತಸ್ರಾವ",
    ]),
    _rule("snake_bite", "EMERGENCY", [
        r"snake ?bite", r"bitten by (?:a )?snake", r"snake (?:has )?bit", r"snake (?:ne )?(?:kaat|kat)",
        r"(?:saanp|saap|sanp|saamp) (?:ne )?(?:kaat|kaata|kaatа|kat|kata|kaat liya|kaat liyaa|dans)",
        r"haavu (?:kacchi|kachchi|kacchide|kachide|kacchitu|kachitu)",
        "साँप ने काट", "सांप ने काट", "ಹಾವು ಕಚ್ಚ",
    ]),
    _rule("poisoning", "EMERGENCY", [
        r"(?:swallowed|drank|drunk|ate|took|consumed|had) (?:some |a lot of |the )?(?:poison|pesticide|insecticide|acid|bleach|phenyl|phenol|rat poison|kerosene|harpic|toilet cleaner)",
        r"(?<!food )poisoned", r"overdose", r"overdosed",
        r"took (?:too many|a lot of|lots of|many|all (?:the|his|her|my)) (?:tablets|pills|sleeping pills|medicines)",
        rf"(?:zeher|zehar|jeher|jahar|zahar) (?:kha|pi|pee) (?:liya|li|lia|gaya|gayi|liyaa)",
        r"(?:keetnashak|keet nashak|poison|dawai|goliyan|goliyaan) (?:pi|kha) (?:liya|li|gaya|gayi)",
        r"visha (?:kudid|kudidi|tind)",
        "ज़हर खा", "जहर खा", "ज़हर पी", "जहर पी", "ವಿಷ ಕುಡಿ",
    ]),
    _rule("anaphylaxis", "EMERGENCY", [
        r"throat (?:is )?(?:swelling|swollen|closing|closing up|tight)", r"(?:tongue|lips|face) (?:is |are )?(?:swelling|swollen) (?:up )?(?:and|after|with)",
        r"anaphyla(?:xis|ctic)", r"severe allergic reaction", r"allergic reaction .{0,30}(?:breath|throat|swell)",
        rf"gala (?:sooj|suj|band ho) {RAHA}", rf"gala (?:sooj|suj) (?:gaya|gya)",
    ]),
    _rule("pregnancy_emergency", "EMERGENCY", [
        r"pregnan\w*.{0,40}(?:bleeding|blood|spotting heavily)", r"(?:bleeding|blood).{0,40}pregnan\w*",
        r"(?:baby|baby's|babys|fetal|foetal) (?:is )?(?:not|isn'?t|stopped) (?:moving|kicking)",
        r"no (?:fetal|foetal|baby) movement", r"(?:baby|bachcha|bacha|bachche) (?:hil|hal) {NAHI}".replace("{NAHI}", NAHI),
        rf"(?:pregnant|garbhvati|garbhwati|pet se).{{0,40}}(?:khoon|khun|bleeding)",
        r"(?:water|waters) (?:broke|broken|has broken|leaking)", r"pani ki thaili fat",
        "गर्भवती.*खून", "ಗರ್ಭಿಣಿ.*ರಕ್ತ",
    ]),
    _rule("self_harm", "EMERGENCY", [
        r"kill(?:ing)? my ?self", r"end(?:ing)? (?:my|it) (?:life|all)", r"take my (?:own )?life",
        r"(?:don'?t|do not|dont) want to (?:live|be alive|go on)", r"want to die", r"wanna die",
        r"suicid(?:e|al)", r"(?:hurt|harm|cut) my ?self", r"no reason to live", r"better off dead",
        r"marna (?:chahta|chahti|chahte)", r"mar (?:jana|jaana) (?:chahta|chahti|chahte)",
        rf"jeene ka (?:mann|man) {NAHI}", r"jeena (?:nahi|nahin) (?:chahta|chahti)",
        r"khud ?(?:khushi|kushi|kashi)", r"aatm ?hatya", r"atm ?hatya", r"apni jaan (?:le|lena|de|dena)",
        r"saayabeku", r"sayabeku", r"saaybeku", r"aatmahatye", r"atmahatye", r"jeeva (?:beda|bedaa)",
        "आत्महत्या", "मरना चाहत", "जीने का मन नहीं", "खुदकुशी", "ಆತ್ಮಹತ್ಯೆ", "ಸಾಯಬೇಕು",
    ], route="TELE_MANAS_AND_HUMAN", negatable=False),
    # ---- URGENT: a nurse within minutes, not an ambulance
    _rule("fever_red_flags", "URGENT", [
        r"(?:fever|temperature).{0,40}(?:confus\w*|stiff neck|not making sense|very drowsy|can'?t stay awake|fits)",
        r"stiff neck.{0,40}(?:fever|temperature)",
        r"(?:child|baby|son|daughter|kid|infant|beta|beti|bachcha|bachche|bacha).{0,40}(?:fever|bukhar|bukhaar).{0,40}(?:rash|spots|daane|chakatte)",
        r"(?:rash|spots).{0,30}(?:fever|bukhar)", r"(?:bukhar|bukhaar).{0,40}(?:gardan akad|behki|bekabu|confus)",
    ]),
    _rule("animal_bite", "URGENT", [
        r"(?:dog|monkey|cat|stray) ?bite", r"bitten by (?:a |the )?(?:dog|monkey|cat|stray)", r"(?:dog|monkey|cat) (?:has )?bit",
        r"(?:kutte|kutta|kuttey|bandar|billi) ne (?:kaat|kat|kaata|kata)", r"(?:nayi|naayi|kothi) kacchi",
        "कुत्ते ने काट", "ನಾಯಿ ಕಚ್ಚ",
    ]),
    _rule("head_injury", "URGENT", [
        r"(?:hit|banged|hurt|injured|cut) (?:his|her|my|their) head", r"head injury", r"fell .{0,30}head",
        r"(?:sir|sar|sirr) (?:par|pe|mein|me) (?:chot|lagi|laga)", r"gir (?:gaye|gayi|gaya|gaye the).{0,30}(?:sir|sar)",
        "सिर पर चोट", "ತಲೆಗೆ ಪೆಟ್ಟು",
    ]),
    _rule("post_surgery_wound", "URGENT", [
        r"(?:stitches|wound|surgery|operation|incision|taanke|tanke).{0,40}(?:pus|opened|opening|open up|bleeding|oozing|infected|very red|swollen and hot)",
        r"(?:taanke|tanke|tanka) (?:khul|kul) (?:gaye|gaya|gayi)", r"(?:pus|pas|peep) (?:aa|nikal) {RAHA}".replace("{RAHA}", RAHA),
    ]),
    _rule("severe_burn", "URGENT", [
        r"(?:badly|severely|seriously) burn(?:ed|t)", r"severe burn", r"burn(?:ed|t) (?:his|her|my|their) (?:face|whole|body|chest)",
        r"(?:buri tarah|bahut) jal (?:gaya|gayi|gaye)", r"aag (?:mein|me|se) jal",
    ]),
)

_NEG_BEFORE = re.compile(r"(?:^|\s)(?:no|not|without|never|denies|deny|koi)$")
_NEG_BEFORE_2 = re.compile(r"(?:don'?t|dont|do not|didn'?t|did not|doesn'?t|does not|never had|no more|not having|not got) (?:(?:have|any|a|an|had|got) )*$")
_NEG_AFTER = re.compile(rf"^\s*{NAHI} (?:hai|he|tha|thi|hua|hui)\b")
_TIER = {"URGENT": 2, "EMERGENCY": 3}


def _negated(text: str, start: int, end: int) -> bool:
    before = text[:start].rstrip()
    if _NEG_BEFORE.search(before) or _NEG_BEFORE_2.search(text[:start]):
        return True
    return bool(_NEG_AFTER.match(text[end:]))


def red_flags(text: str) -> list[RedFlag]:
    """Every red flag in one caller message, most urgent first. Empty list when none."""
    if not text:
        return []
    t = normalise(text)
    hits: list[RedFlag] = []
    for rule in RULES:
        for pat in rule.patterns:
            m = pat.search(t)
            if m and not (rule.negatable and _negated(t, m.start(), m.end())):
                hits.append(RedFlag(rule.id, rule.tier, rule.route, m.group(0)))
                break
    hits.sort(key=lambda h: -_TIER[h.tier])
    return hits


def top_flag(text: str) -> RedFlag | None:
    hits = red_flags(text)
    if not hits:
        return None
    # self-harm keeps its own route even next to a physical red flag (Tele-MANAS must be given)
    sh = next((h for h in hits if h.route == "TELE_MANAS_AND_HUMAN"), None)
    return sh or hits[0]


# ---------------------------------------------------------------- the human exit

_HUMAN_WORDS = r"(?:human|humans|real person|a person|person|someone|somebody|staff|receptionist|operator|agent|representative|nurse|manager|insaan|insan|aadmi|admi|banda|vyakti)"
_HUMAN_PATTERNS = [re.compile(p) for p in (
    rf"\b(?:talk|speak|connect|transfer|put) (?:me )?(?:to|with|through to) (?:a |an |the |some |any |real |actual |live |your )*{_HUMAN_WORDS}\b",
    rf"\b(?:want|need|get) (?:a |an |to talk to a |to speak to a |to speak with a |to talk with a )?(?:real |actual |live )?(?:human|real person|person|staff member|receptionist|operator)\b",
    r"\b(?:are you|is this) a (?:robot|bot|machine|computer)\b.*\b(?:human|person|staff)\b",
    rf"\b(?:insaan|insan|aadmi|admi|banda|staff|receptionist|operator|kisi|asli (?:insaan|insan|aadmi)) (?:se|say) (?:baat|bat|baath)\b",
    r"\b(?:baat|bat) (?:karao|karwao|karvao|karwaiye|karaiye|kara do|karwa do)\b.{0,20}\b(?:insaan|insan|aadmi|staff|kisi)\b",
    r"\b(?:manushya|vyakti|staff) (?:jothe|jote|hatra) (?:maatadbeku|matadbeku|maatanaadu)\b",
    r"इंसान से बात|किसी से बात करा|स्टाफ से बात",
    r"ಮನುಷ್ಯರ ಜೊತೆ ಮಾತನಾಡ|ಸಿಬ್ಬಂದಿ ಜೊತೆ",
)]
_FILLER = r"(?:please|pls|plz|ji|sir|madam|ma'am|mam|jaldi|abhi|turant|now|right now|immediately|quickly|urgently|fast|" \
          r"bhai|bhaiya|didi|haan|ok|okay|hello|hi|yaar)"
_BARE_HUMAN = re.compile(rf"^(?:(?:{_FILLER}|i want|i need|give me|connect me|connect|mujhe|chahiye)\s+)*"
                         r"(?:staff|human|operator|receptionist|representative|insaan|insan|real person)"
                         rf"(?:\s+(?:{_FILLER}|chahiye|do|dijiye|se baat|se baat karao|se baat karwao|bulao|bulaiye))*$")


def wants_human(text: str) -> bool:
    t = normalise(text)
    if not t:
        return False
    if _BARE_HUMAN.match(t):
        return True
    return any(p.search(t) for p in _HUMAN_PATTERNS)


# ---------------------------------------------------------------- recording consent

_NO_RECORDING = [re.compile(p) for p in (
    r"\b(?:don'?t|dont|do not|please don'?t|pls dont) record\b",
    r"\b(?:no|stop|stop the|turn off|switch off|turn off the|switch off the|without) recording\b",
    r"\bi (?:don'?t|do not|dont) (?:consent|agree) to (?:the |this |being )?record",
    r"\bnot (?:ok|okay|fine|comfortable) (?:with|to|about) (?:the |this |being )?record",
    r"\brecord(?:ing)? (?:mat|na|nahi|nahin|band) (?:karo|kariye|kijiye|karna|karein|karen|kar)\b",
    r"\brecording (?:nahi|nahin|mat) (?:chahiye|karo|kariye)\b",
    r"\brecording band\b", r"\brecord (?:nahi|nahin) (?:karna|karo|chahiye)\b",
    r"\brecord (?:maadbedi|madbedi|maadabedi)\b",
    r"रिकॉर्ड मत|रिकॉर्डिंग बंद|ರೆಕಾರ್ಡ್ ಮಾಡಬೇಡಿ",
)]


def declines_recording(text: str) -> bool:
    t = normalise(text)
    return any(p.search(t) for p in _NO_RECORDING)


# ---------------------------------------------------------------- prompt injection (logged, then ignored)

_INJECTION = [re.compile(p) for p in (
    r"\bignore (?:all |any |your |the |my |these |those |previous |prior |above |earlier |every )*(?:instructions|instruction|rules|prompts?|guidelines|directions)\b",
    r"\b(?:forget|disregard|override|bypass) (?:all |any |your |the |previous |prior |earlier )*(?:instructions|rules|prompts?|guidelines|programming|restrictions)\b",
    r"\b(?:reveal|show|tell|print|repeat|give|read|share) (?:me )?(?:your|the) (?:full |whole |exact |hidden |secret )?(?:system )?(?:prompt|instructions|rules|guidelines)\b",
    r"\bsystem prompt\b", r"\bdeveloper mode\b", r"\bjailbreak\b", r"\bdan mode\b",
    r"\b(?:you are now|from now on you are|act as|pretend (?:to be|you are)) (?:a |an |my )?(?:doctor|developer|admin|administrator|different|unrestricted|new)\b",
    r"\b(?:instructions|rules) (?:bhool|bhul|ignore) (?:jao|ja|karo|do)\b",
    r"\b(?:apne|apna|sare|saare) (?:instructions|rules|niyam) (?:bhool|bhul|ignore)\b",
)]


def injection_attempt(text: str) -> bool:
    t = normalise(text)
    return any(p.search(t) for p in _INJECTION)


# ---------------------------------------------------------------- a clear yes

_YES = re.compile(r"\b(?:yes|yeah|yea|yep|yup|ya|yah|haan|haa|han|ha|haanji|hanji|haan ji|ji haan|ji|jee|theek|thik|theek hai|thik hai|"
                  r"ok|okay|okk|okey|k|sure|confirm|confirmed|correct|right|sahi|bilkul|kar do|kardo|kar dijiye|kar dena|"
                  r"go ahead|please do|perfect|done|fine|absolutely|of course|exactly|houdu|haudu|sari|saari|aagli|aaytu|ayitu)\b")
_YES_NATIVE = re.compile("हाँ|हां|जी हाँ|ठीक है|ಹೌದು|ಸರಿ")  # no \b: combining marks are not word characters
_NO = re.compile(r"\b(?:no|nope|nah|not|nahi|nahin|nai|nhi|mat|wait|hold on|ruko|ruk jao|ruk|rukiye|change|different|another|other|"
                 r"but|lekin|magar|actually|instead|galat|wrong|illa|beda|bedaa|don'?t|dont|never|later|baad mein)\b")
_NO_NATIVE = re.compile("नहीं|मत |रुको|रुकिए|ಇಲ್ಲ|ಬೇಡ")
_NOT_A_NO = re.compile(r"\b(?:no problem|no problems|no issue|no issues|not a problem|koi baat nahi|koi problem nahi|koi dikkat nahi|"
                       r"why not|no worries|nothing else)\b")


def is_clear_yes(text: str) -> bool:
    """True only for an unambiguous yes ("haan, kar do", "yes please"), never for "yes but…" or "no"."""
    t = normalise(text)
    if not t or len(t.split()) > 14:
        return False
    if "?" in text:
        return False
    stripped = _NOT_A_NO.sub(" ", t)
    if _NO.search(stripped) or _NO_NATIVE.search(t + " "):
        return False
    return bool(_YES.search(t) or _YES_NATIVE.search(t))


# ---------------------------------------------------------------- language

_HI_LATN = re.compile(r"\b(?:hai|hain|hoon|hu|mein|mera|meri|mere|mujhe|muje|aap|aapka|aapki|kya|kyun|nahi|nahin|haan|kar|karo|"
                      r"kariye|karna|chahiye|chahta|chahti|wala|wali|kal|parso|abhi|thoda|bahut|bhi|toh|ji|dard|bukhar|"
                      r"seene|saans|baat|insaan|dijiye|batao|bataiye|raha|rahi|gaya|gayi|liye|ke|ki|ka|se|ko|aur|yeh|woh|"
                      r"theek|accha|achha|bhai|beta|beti|papa|mummy|dawai)\b")
_KN_LATN = re.compile(r"\b(?:novu|illa|beku|beda|maadi|madi|ide|idhe|nanna|nange|nimma|swalpa|houdu|haudu|yenu|enu|hege|"
                      r"yaake|banni|helthini|helu|appointment beku|doctor beku|aagutte|aaytu|kacchi|usiru)\b")


def guess_language(text: str) -> str:
    """'kn', 'hi' (Devanagari), 'hi-en' (romanised Hindi / Hinglish) or 'en'."""
    if re.search(r"[ಀ-೿]", text):
        return "kn"
    if re.search(r"[ऀ-ॿ]", text):
        return "hi"
    t = normalise(text)
    words = max(1, len(t.split()))
    kn = len(_KN_LATN.findall(t))
    hi = len(_HI_LATN.findall(t))
    if kn and kn >= hi:
        return "kn"
    if hi >= 2 or (hi and words <= 4):
        return "hi-en"
    return "en"
