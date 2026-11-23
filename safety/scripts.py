"""Fixed lines Sahay says without asking the model (BLUEPRINT §4.6, §7.1, §7.2).

Emergency and safety wording must never depend on an LLM. Stage 4 caches these as audio.
Languages: en, hi-en (romanised Hinglish), hi (Devanagari), kn (Kannada).
The hi and kn versions need a native-speaker check before the finale.
"""

from __future__ import annotations

import os

HOSPITAL = os.getenv("HOSPITAL_NAME", "Sunrise Multispeciality Hospital")
SHORT = HOSPITAL.split()[0] + " Hospital" if len(HOSPITAL.split()) > 1 else HOSPITAL

LANGS = ("en", "hi-en", "hi", "kn")


def pick(table: dict[str, str], lang: str) -> str:
    return table.get(lang) or table["en"]


def greeting(lang: str = "hi-en", channel: str = "voice") -> str:
    """AI disclosure + recording notice + the human exit, in one breath (≤ 6 s spoken)."""
    voice = channel == "voice"
    return pick({
        "hi-en": f"Namaste, {SHORT}. Main Sahay hoon, hospital ki AI assistant. "
                 + ("Yeh call aapki madad ke liye record ho rahi hai. " if voice else "Yeh chat aapki madad ke liye save ho rahi hai. ")
                 + "Kabhi bhi 'staff' boliye toh main aapko insaan se jod doongi. Bataiye, kaise madad karoon?",
        "en": f"Hello, {SHORT}. I'm Sahay, the hospital's AI assistant. "
              + ("This call is recorded to help you. " if voice else "This chat is saved to help you. ")
              + "Say 'staff' at any time to reach a person. How can I help?",
        "hi": f"नमस्ते, {SHORT}। मैं सहाय हूँ, अस्पताल की AI सहायक। "
              + ("यह कॉल आपकी मदद के लिए रिकॉर्ड हो रही है। " if voice else "यह चैट आपकी मदद के लिए सेव हो रही है। ")
              + "कभी भी 'स्टाफ' बोलिए तो मैं आपको किसी इंसान से जोड़ दूँगी। बताइए, कैसे मदद करूँ?",
        "kn": f"ನಮಸ್ಕಾರ, {SHORT}. ನಾನು ಸಹಾಯ್, ಆಸ್ಪತ್ರೆಯ AI ಸಹಾಯಕಿ. "
              + ("ನಿಮ್ಮ ಸಹಾಯಕ್ಕಾಗಿ ಈ ಕರೆ ರೆಕಾರ್ಡ್ ಆಗುತ್ತಿದೆ. " if voice else "ನಿಮ್ಮ ಸಹಾಯಕ್ಕಾಗಿ ಈ ಚಾಟ್ ಉಳಿಸಲಾಗುತ್ತಿದೆ. ")
              + "ಯಾವಾಗ ಬೇಕಾದರೂ 'ಸ್ಟಾಫ್' ಎಂದು ಹೇಳಿ, ನಾನು ನಿಮ್ಮನ್ನು ಸಿಬ್ಬಂದಿಗೆ ಸಂಪರ್ಕಿಸುತ್ತೇನೆ. ಹೇಳಿ, ಹೇಗೆ ಸಹಾಯ ಮಾಡಲಿ?",
    }, lang)


EMERGENCY = {
    "en": "This could be an emergency. Please call 108 for an ambulance right now, or 112, or come straight to our "
          "Emergency department. I'm connecting you to our emergency nurse now, please stay on the line.",
    "hi-en": "Yeh emergency ho sakti hai. Abhi turant 108 par ambulance ke liye call kariye, ya 112 par, ya seedha "
             "hamare Emergency mein aaiye. Main aapko abhi emergency nurse se jod rahi hoon, line par bane rahiye.",
    "hi": "यह इमरजेंसी हो सकती है। अभी तुरंत 108 पर एम्बुलेंस के लिए कॉल कीजिए, या 112 पर, या सीधे हमारे इमरजेंसी "
          "विभाग में आइए। मैं आपको अभी इमरजेंसी नर्स से जोड़ रही हूँ, लाइन पर बने रहिए।",
    "kn": "ಇದು ತುರ್ತು ಪರಿಸ್ಥಿತಿ ಆಗಿರಬಹುದು. ದಯವಿಟ್ಟು ಈಗಲೇ ಆಂಬುಲೆನ್ಸ್‌ಗಾಗಿ 108 ಅಥವಾ 112 ಗೆ ಕರೆ ಮಾಡಿ, ಅಥವಾ ನೇರವಾಗಿ "
          "ನಮ್ಮ ತುರ್ತು ವಿಭಾಗಕ್ಕೆ ಬನ್ನಿ. ನಾನು ನಿಮ್ಮನ್ನು ಈಗಲೇ ತುರ್ತು ನರ್ಸ್‌ಗೆ ಸಂಪರ್ಕಿಸುತ್ತಿದ್ದೇನೆ, ದಯವಿಟ್ಟು ಲೈನ್‌ನಲ್ಲಿ ಇರಿ.",
}

SELF_HARM = {
    "en": "Thank you for telling me. You don't have to face this alone. Please call Tele-MANAS on 14416, it's free and "
          "open day and night, and if you are in danger right now, call 112. I'm connecting you to a person from our "
          "team now, please stay on the line.",
    "hi-en": "Aapne mujhe bataya, yeh bahut achha kiya. Aap akele nahi hain. Tele-MANAS ko 14416 par call kariye, yeh "
             "free hai aur din-raat khula hai, aur agar abhi khatra hai toh 112 par call kariye. Main aapko abhi hamari "
             "team ke ek insaan se jod rahi hoon, line par bane rahiye.",
    "hi": "आपने मुझे बताया, यह बहुत अच्छा किया। आप अकेले नहीं हैं। टेली-मानस को 14416 पर कॉल कीजिए, यह मुफ़्त है "
          "और दिन-रात खुला है, और अगर अभी खतरा है तो 112 पर कॉल कीजिए। मैं आपको अभी हमारी टीम के एक इंसान से जोड़ रही हूँ, "
          "लाइन पर बने रहिए।",
    "kn": "ನನಗೆ ಹೇಳಿದ್ದಕ್ಕೆ ಧನ್ಯವಾದಗಳು. ನೀವು ಒಬ್ಬರೇ ಇಲ್ಲ. ದಯವಿಟ್ಟು ಟೆಲಿ-ಮನಸ್‌ಗೆ 14416 ಗೆ ಕರೆ ಮಾಡಿ, ಇದು ಉಚಿತ "
          "ಮತ್ತು ಹಗಲು ರಾತ್ರಿ ಲಭ್ಯವಿದೆ. ಈಗಲೇ ಅಪಾಯವಿದ್ದರೆ 112 ಗೆ ಕರೆ ಮಾಡಿ. ನಾನು ನಿಮ್ಮನ್ನು ಈಗಲೇ ನಮ್ಮ ತಂಡದ ಒಬ್ಬರಿಗೆ "
          "ಸಂಪರ್ಕಿಸುತ್ತಿದ್ದೇನೆ, ದಯವಿಟ್ಟು ಲೈನ್‌ನಲ್ಲಿ ಇರಿ.",
}

URGENT = {
    "en": "I want a nurse to hear about this straight away. I'm connecting you to our triage nurse now. If things get "
          "worse at any point, call 108 for an ambulance.",
    "hi-en": "Main chahti hoon ki nurse yeh turant sune. Main aapko abhi hamari triage nurse se jod rahi hoon. Agar "
             "kabhi bhi haalat bigde, toh 108 par ambulance ke liye call kariye.",
    "hi": "मैं चाहती हूँ कि नर्स इसे तुरंत सुने। मैं आपको अभी हमारी ट्राइएज नर्स से जोड़ रही हूँ। अगर कभी भी हालत बिगड़े, "
          "तो 108 पर एम्बुलेंस के लिए कॉल कीजिए।",
    "kn": "ಇದನ್ನು ನರ್ಸ್ ತಕ್ಷಣ ಕೇಳಬೇಕು. ನಾನು ನಿಮ್ಮನ್ನು ಈಗಲೇ ನಮ್ಮ ಟ್ರಯಾಜ್ ನರ್ಸ್‌ಗೆ ಸಂಪರ್ಕಿಸುತ್ತಿದ್ದೇನೆ. ಯಾವಾಗಲಾದರೂ "
          "ಸ್ಥಿತಿ ಹದಗೆಟ್ಟರೆ, ಆಂಬುಲೆನ್ಸ್‌ಗಾಗಿ 108 ಗೆ ಕರೆ ಮಾಡಿ.",
}

# Said again on every later turn of an escalated emergency call, until a human takes over.
EMERGENCY_REPEAT = {
    "en": "Please call 108 now if you haven't already. Our emergency team has been told and someone will be with you shortly.",
    "hi-en": "Agar abhi tak nahi kiya hai toh kripya abhi 108 par call kariye. Hamari emergency team ko bata diya gaya hai, "
             "koi abhi aapse baat karega.",
    "hi": "अगर अभी तक नहीं किया है तो कृपया अभी 108 पर कॉल कीजिए। हमारी इमरजेंसी टीम को बता दिया गया है, कोई अभी आपसे बात करेगा।",
    "kn": "ಇನ್ನೂ ಮಾಡದಿದ್ದರೆ ದಯವಿಟ್ಟು ಈಗಲೇ 108 ಗೆ ಕರೆ ಮಾಡಿ. ನಮ್ಮ ತುರ್ತು ತಂಡಕ್ಕೆ ತಿಳಿಸಲಾಗಿದೆ, ಯಾರಾದರೂ ಬೇಗ ನಿಮ್ಮೊಂದಿಗೆ ಮಾತನಾಡುತ್ತಾರೆ.",
}

SELF_HARM_REPEAT = {
    "en": "I'm still here with you. A person from our team is joining. You can call Tele-MANAS on 14416 any time, "
          "and 112 if you are in danger.",
    "hi-en": "Main aapke saath hoon. Hamari team ka ek insaan jud raha hai. Tele-MANAS ko kabhi bhi 14416 par call "
             "kar sakte hain, aur khatra ho toh 112 par.",
    "hi": "मैं आपके साथ हूँ। हमारी टीम का एक इंसान जुड़ रहा है। टेली-मानस को कभी भी 14416 पर कॉल कर सकते हैं, "
          "और खतरा हो तो 112 पर।",
    "kn": "ನಾನು ನಿಮ್ಮ ಜೊತೆ ಇದ್ದೇನೆ. ನಮ್ಮ ತಂಡದ ಒಬ್ಬರು ಸೇರುತ್ತಿದ್ದಾರೆ. ಯಾವಾಗ ಬೇಕಾದರೂ ಟೆಲಿ-ಮನಸ್‌ಗೆ 14416 ಗೆ "
          "ಕರೆ ಮಾಡಬಹುದು, ಅಪಾಯವಿದ್ದರೆ 112 ಗೆ.",
}

HUMAN = {
    "en": "Of course. I'm connecting you to our staff now, please stay on the line.",
    "hi-en": "Zaroor. Main aapko abhi staff se jod rahi hoon, line par bane rahiye.",
    "hi": "ज़रूर। मैं आपको अभी स्टाफ से जोड़ रही हूँ, लाइन पर बने रहिए।",
    "kn": "ಖಂಡಿತ. ನಾನು ನಿಮ್ಮನ್ನು ಈಗಲೇ ನಮ್ಮ ಸಿಬ್ಬಂದಿಗೆ ಸಂಪರ್ಕಿಸುತ್ತಿದ್ದೇನೆ, ದಯವಿಟ್ಟು ಲೈನ್‌ನಲ್ಲಿ ಇರಿ.",
}

# The transfer itself failed: say so, and promise only what was actually set up.
TRANSFER_FAILED = {
    "en": "I'm sorry, I can't connect you to a person right this moment. I've asked our staff to call you back on "
          "this number as soon as possible.",
    "hi-en": "Maaf kijiye, abhi main aapko kisi insaan se nahi jod pa rahi. Maine staff ko bol diya hai, woh aapko "
             "isi number par jald se jald call karenge.",
    "hi": "माफ़ कीजिए, अभी मैं आपको किसी इंसान से नहीं जोड़ पा रही। मैंने स्टाफ को बोल दिया है, वो आपको इसी नंबर पर "
          "जल्द से जल्द कॉल करेंगे।",
    "kn": "ಕ್ಷಮಿಸಿ, ಈ ಕ್ಷಣ ನಿಮ್ಮನ್ನು ಸಿಬ್ಬಂದಿಗೆ ಸಂಪರ್ಕಿಸಲು ಆಗುತ್ತಿಲ್ಲ. ಇದೇ ನಂಬರ್‌ಗೆ ಆದಷ್ಟು ಬೇಗ ಕರೆ ಮಾಡುವಂತೆ "
          "ಸಿಬ್ಬಂದಿಗೆ ತಿಳಿಸಿದ್ದೇನೆ.",
}

FILLER = {
    "en": "One moment, let me check.",
    "hi-en": "Ek second, main check karti hoon.",
    "hi": "एक सेकंड, मैं चेक करती हूँ।",
    "kn": "ಒಂದು ಕ್ಷಣ, ನಾನು ಪರಿಶೀಲಿಸುತ್ತೇನೆ.",
}

# The model failed twice: be honest, and make sure someone calls back.
LLM_DOWN = {
    "en": "I'm sorry, I'm having trouble on my side right now. I've asked our front desk to call you back on this "
          "number shortly. If it's urgent, say 'staff' and I'll connect you to a person.",
    "hi-en": "Maaf kijiye, abhi mere system mein dikkat aa rahi hai. Maine front desk ko bol diya hai, woh aapko isi "
             "number par jaldi call karenge. Agar urgent hai toh 'staff' boliye, main aapko insaan se jod doongi.",
    "hi": "माफ़ कीजिए, अभी मेरे सिस्टम में दिक्कत आ रही है। मैंने फ्रंट डेस्क को बोल दिया है, वो आपको इसी नंबर पर जल्दी कॉल करेंगे। "
          "अगर ज़रूरी है तो 'स्टाफ' बोलिए, मैं आपको इंसान से जोड़ दूँगी।",
    "kn": "ಕ್ಷಮಿಸಿ, ಈಗ ನನ್ನ ಕಡೆ ತೊಂದರೆ ಆಗುತ್ತಿದೆ. ಫ್ರಂಟ್ ಡೆಸ್ಕ್‌ಗೆ ತಿಳಿಸಿದ್ದೇನೆ, ಅವರು ಇದೇ ನಂಬರ್‌ಗೆ ಬೇಗ ಕರೆ ಮಾಡುತ್ತಾರೆ. "
          "ತುರ್ತಾಗಿದ್ದರೆ 'ಸ್ಟಾಫ್' ಎಂದು ಹೇಳಿ.",
}

# The model declined to answer (refusal stop reason): no retry, offer a person.
CANNOT_HELP = {
    "en": "I'm sorry, I can't help with that on this call. I can connect you to our staff if you like.",
    "hi-en": "Maaf kijiye, is call par main ismein madad nahi kar sakti. Aap chahein toh main aapko staff se jod sakti hoon.",
    "hi": "माफ़ कीजिए, इस कॉल पर मैं इसमें मदद नहीं कर सकती। आप चाहें तो मैं आपको स्टाफ से जोड़ सकती हूँ।",
    "kn": "ಕ್ಷಮಿಸಿ, ಈ ಕರೆಯಲ್ಲಿ ನಾನು ಇದಕ್ಕೆ ಸಹಾಯ ಮಾಡಲು ಆಗುವುದಿಲ್ಲ. ಬೇಕಿದ್ದರೆ ನಿಮ್ಮನ್ನು ಸಿಬ್ಬಂದಿಗೆ ಸಂಪರ್ಕಿಸುತ್ತೇನೆ.",
}

RECORDING_OFF = {
    "en": "Understood, I've stopped recording this call.",
    "hi-en": "Theek hai, maine is call ki recording band kar di hai.",
    "hi": "ठीक है, मैंने इस कॉल की रिकॉर्डिंग बंद कर दी है।",
    "kn": "ಸರಿ, ಈ ಕರೆಯ ರೆಕಾರ್ಡಿಂಗ್ ನಿಲ್ಲಿಸಿದ್ದೇನೆ.",
}

HOW_CAN_I_HELP = {
    "en": "How can I help?",
    "hi-en": "Bataiye, kaise madad karoon?",
    "hi": "बताइए, कैसे मदद करूँ?",
    "kn": "ಹೇಳಿ, ಹೇಗೆ ಸಹಾಯ ಮಾಡಲಿ?",
}

# L3 replacements, one per blocked category (BLUEPRINT §7.2)
SAFE_REPLACEMENT: dict[str, dict[str, str]] = {
    "diagnosis": {
        "en": "I'm not able to say what might be causing that, as I'm not a doctor. I can book you with the right "
              "doctor, or ask a nurse to call you.",
        "hi-en": "Main doctor nahi hoon, isliye main nahi bata sakti ki yeh kis wajah se ho raha hai. Main aapko sahi "
                 "doctor ke saath appointment dila sakti hoon, ya nurse se call karwa sakti hoon.",
        "hi": "मैं डॉक्टर नहीं हूँ, इसलिए मैं नहीं बता सकती कि यह किस वजह से हो रहा है। मैं आपको सही डॉक्टर के साथ "
              "अपॉइंटमेंट दिला सकती हूँ, या नर्स से कॉल करवा सकती हूँ।",
        "kn": "ನಾನು ವೈದ್ಯರಲ್ಲ, ಹಾಗಾಗಿ ಇದಕ್ಕೆ ಕಾರಣ ಏನು ಎಂದು ಹೇಳಲು ಆಗುವುದಿಲ್ಲ. ಸರಿಯಾದ ವೈದ್ಯರ ಅಪಾಯಿಂಟ್‌ಮೆಂಟ್ "
              "ಮಾಡಿಕೊಡಬಹುದು, ಅಥವಾ ನರ್ಸ್ ಕರೆ ಮಾಡುವಂತೆ ಹೇಳಬಹುದು.",
    },
    "medication": {
        "en": "I can't advise on medicines or doses. I'll ask a nurse to call you back about it.",
        "hi-en": "Main dawai ya dose ke baare mein salah nahi de sakti. Main nurse se aapko call karwa deti hoon.",
        "hi": "मैं दवाई या डोज़ के बारे में सलाह नहीं दे सकती। मैं नर्स से आपको कॉल करवा देती हूँ।",
        "kn": "ಔಷಧಿ ಅಥವಾ ಡೋಸ್ ಬಗ್ಗೆ ನಾನು ಸಲಹೆ ನೀಡಲು ಆಗುವುದಿಲ್ಲ. ನರ್ಸ್ ನಿಮಗೆ ಕರೆ ಮಾಡುವಂತೆ ಹೇಳುತ್ತೇನೆ.",
    },
    "results": {
        "en": "I can only tell you whether a report is ready, not what it says. The doctor will go through the results with you.",
        "hi-en": "Main sirf bata sakti hoon ki report ready hai ya nahi. Result aapko doctor hi samjhayenge.",
        "hi": "मैं सिर्फ़ बता सकती हूँ कि रिपोर्ट तैयार है या नहीं। नतीजे आपको डॉक्टर ही समझाएँगे।",
        "kn": "ವರದಿ ಸಿದ್ಧವಾಗಿದೆಯೇ ಎಂದು ಮಾತ್ರ ಹೇಳಬಲ್ಲೆ. ಫಲಿತಾಂಶವನ್ನು ವೈದ್ಯರೇ ವಿವರಿಸುತ್ತಾರೆ.",
    },
    "promise": {
        "en": "I can't promise that, but I'll make sure the right person follows up with you.",
        "hi-en": "Main iska vaada nahi kar sakti, lekin main pakka karungi ki sahi vyakti aapse sampark kare.",
        "hi": "मैं इसका वादा नहीं कर सकती, लेकिन मैं पक्का करूँगी कि सही व्यक्ति आपसे संपर्क करे।",
        "kn": "ಅದರ ಭರವಸೆ ಕೊಡಲು ಆಗುವುದಿಲ್ಲ, ಆದರೆ ಸರಿಯಾದ ವ್ಯಕ್ತಿ ನಿಮ್ಮನ್ನು ಸಂಪರ್ಕಿಸುವಂತೆ ನೋಡಿಕೊಳ್ಳುತ್ತೇನೆ.",
    },
    "leak": {
        "en": "Sorry, I can't share that. How else can I help you today?",
        "hi-en": "Maaf kijiye, main yeh share nahi kar sakti. Aur kaise madad karoon?",
        "hi": "माफ़ कीजिए, मैं यह शेयर नहीं कर सकती। और कैसे मदद करूँ?",
        "kn": "ಕ್ಷಮಿಸಿ, ಅದನ್ನು ಹಂಚಿಕೊಳ್ಳಲು ಆಗುವುದಿಲ್ಲ. ಇನ್ನೇನು ಸಹಾಯ ಮಾಡಲಿ?",
    },
    "sensitive": {
        "en": "For your privacy, I won't read that number out.",
        "hi-en": "Aapki privacy ke liye, main woh number nahi padhungi.",
        "hi": "आपकी प्राइवेसी के लिए, मैं वह नंबर नहीं पढ़ूँगी।",
        "kn": "ನಿಮ್ಮ ಗೌಪ್ಯತೆಗಾಗಿ, ಆ ಸಂಖ್ಯೆಯನ್ನು ನಾನು ಓದುವುದಿಲ್ಲ.",
    },
}
