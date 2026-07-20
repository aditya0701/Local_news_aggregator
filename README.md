# TechDrishti (Techदृष्टि) 🪔

**हिंदी में प्रीमियम साइंस और टेक्नोलॉजी जर्नल**

📄 **[Read the English engineering case study →](README.en.md)** — architecture, real measured
eval numbers (triage accuracy, judge-validated entity/query quality, hallucination rate), and
honestly-stated limitations. This document stays the product-facing Hindi identity.

> *दृष्टि — वह नज़र जो जानकारी को ज्ञान में बदल दे।*

TechDrishti एक AI-संचालित (AI-driven) न्यूज़ एग्रीगेटर है, जो दुनिया की सबसे अच्छी साइंस, टेक्नोलॉजी, AI, गेम डेवलपमेंट और स्पेस से जुड़ी ख़बरों को भारतीय पाठकों तक उन्हीं की भाषा "हिंदी" में, एक प्रीमियम पढ़ने के अनुभव (reading experience) के साथ पहुँचाता है।

---

## 🎯 विज़न (Vision)

भारतीय छात्रों और पाठकों के लिए इंटरनेट पर उच्च-गुणवत्ता वाले साइंस और टेक कंटेंट की भारी कमी है — ख़ासकर हिंदी में। ज़्यादातर अच्छी जानकारी अंग्रेज़ी स्रोतों (TechCrunch, NASA, Space.com आदि) तक सीमित रह जाती है।

TechDrishti इस गैप को भरने की कोशिश है। यह न सिर्फ़ अनुवाद करता है, बल्कि अंग्रेज़ी लेखों से **वैज्ञानिक तथ्यों (hard facts)** को निकालकर, एक ओपन-सोर्स AI मॉडल की मदद से उन तथ्यों पर आधारित एक **बिल्कुल नया और ओरिजिनल हिंदी आर्टिकल** लिखता है — जिससे कॉपीराइट का उल्लंघन नहीं होता, और पाठक को formal, भरोसेमंद हिंदी में जानकारी मिलती है।

> ध्यान देने योग्य बात: यह स्ट्रैटेजी इसे कॉपीराइट जोखिम से पूरी तरह मुक्त नहीं करती — स्रोत लेखों के क्रेडिट/लिंक और तथ्य-सत्यापन (fact-checking) की प्रक्रिया को पारदर्शी रखना ज़रूरी है।

### फ़िलहाल का स्कोप (Current Phase)

प्रोजेक्ट को चरणों (phases) में बढ़ाया जा रहा है:

1. **Phase 1 (अभी)** — AI और कंप्यूटर/टेक्नोलॉजी से जुड़ी ख़बरें। उद्देश्य: AI से जुड़ा नया ज्ञान आम लोगों तक पहुँचाना, जिसे लेखक स्वयं भी उपयोग में ला सके।
2. **Phase 2** — गेम डेवलपमेंट (Unity, Unreal Engine) और VR से जुड़ा कंटेंट।
3. **Phase 3** — स्पेस (Space) से जुड़ी ख़बरें और खोजें।

हर फ़ेज़ पिछले फ़ेज़ की नींव पर बनेगा ताकि पाइपलाइन और UI स्थिर रहें, और स्कोप धीरे-धीरे चौड़ा हो।

---

## 🏗️ आर्किटेक्चर (Architecture)

प्रोजेक्ट पूरी तरह **GitHub Actions** पर चलता है — कोई सर्वर, कोई होस्टिंग कॉस्ट नहीं:

```
RSS Feeds + GitHub Trending
        │
        ▼
  collectors/            ──▶  नए आर्टिकल्स इकट्ठे करता है, dedupe करता है
        │
        ▼
  writer/cluster.py      ──▶  समान विषय वाले आर्टिकल्स को एक साथ ग्रुप करता है
        │
        ▼
  writer/synthesize.py   ──▶  4-चरण AI पाइपलाइन — Sarvam AI (sarvam-30b + sarvam-105b)
        │                     (पूरी इंजीनियरिंग डिटेल नीचे ↓)
        │
        ├──▶ सफल        → मूल, context-aware हिंदी आर्टिकल
        └──▶ SKIP/असफल  → translator/translate.py (Google Translate fallback)
        │
        ▼
  output/articles_hindi.json   ──▶  GitHub push (Actions bot commit)
        │
        ▼
  frontend/ (index.html + article.html) — Vanilla HTML/CSS/JS, GitHub Pages पर डिप्लॉय
```

पाइपलाइन रोज़ 8 AM IST पर `.github/workflows/daily.yml` के cron से अपने-आप चलती है (`workflow_dispatch` से मैन्युअल ट्रिगर भी संभव)।

> 👇 यह पाइपलाइन सिर्फ़ *translate* नहीं करती — यह एक context-aware writing agent है। कैसे, इसका पूरा ब्रेकडाउन नीचे है।

---

## 🧠 इंजीनियरिंग हाइलाइट्स: जेनेरिक ट्रांसलेटर बनाम कॉन्टेक्स्ट-अवेयर राइटर

रेपो में असल में अंग्रेज़ी आर्टिकल को हिंदी में बदलने के दो तरीक़े मौजूद हैं, और इन दोनों के बीच का फ़र्क़ ही सबसे साफ़ तरीक़े से दिखाता है कि यहाँ इंजीनियरिंग कहाँ हुई है।

**जेनेरिक बेसलाइन — `translator/translate.py`।** एक टिपिकल "आर्टिकल ट्रांसलेट करो" प्रोजेक्ट ऐसा ही दिखता है: हर फ़ील्ड को Google Translate से गुज़ारो, प्रॉपर नाउन को regex से पहचानो ताकि वो बिगड़ें नहीं, बस। इसे जानबूझकर कोडबेस में रखा गया है — AI पाइपलाइन के फेल होने पर fallback path के तौर पर — जिससे यह सिर्फ़ एक थ्योरी नहीं बल्कि एक ज़िंदा बेसलाइन बन जाता है, जिससे असली पाइपलाइन की तुलना की जा सके।

```python
# translator/translate.py
def translate_item(item: dict, language: str = "hindi") -> dict:
    lang_code = LANGUAGES.get(language, "hi")
    translator = GoogleTranslator(source="auto", target=lang_code)
    title = _safe_translate(translator, item["title"])
    summary = _safe_translate(translator, item["summary"])
    ...
```

पिछले आर्टिकल्स की कोई मेमोरी नहीं, तथ्यों की कोई क्यूरेशन नहीं, हर स्टोरी के लिए एक जैसा टोन। `writer/synthesize.py` इसी की कमियों को भरने के लिए बनाया गया है।

### Stage 0 — कोई भी टोकन खर्च करने से पहले कंटेंट की सच्चाई जाँचो

```python
source_text = scrape_source(source_url)
if len(source_text) + len(summary) < 250:
    trace["outcome"] = "skipped_no_content"
    return SKIP
```

सस्ता, deterministic चेक — जिससे किसी खाली पेज पर LLM कॉल का पैसा बर्बाद न हो।

### Stage 1 — महँगे मॉडल से पहले एक सस्ता मॉडल triage करता है, दो कॉल्स में

`sarvam-30b` (तेज़/सस्ता) आर्टिकल को पढ़कर दो काम करता है: तय करता है कि यह असल में टेक न्यूज़ है या नहीं (जॉब पोस्टिंग नहीं, "Show HN" लिस्टिंग नहीं), और उसमें शामिल एंटिटीज़ को उनके टाइप के साथ निकालता है। यह दो अलग-अलग कॉल्स में होता है, एक ही कॉल में नहीं — क्योंकि एक कॉल में यह पाया गया कि reasoning कभी-कभी पूरा टोकन बजट खा जाता था और JSON लिखने के लिए कुछ नहीं बचता था (एक ही प्रॉम्प्ट को 3 बार चलाने पर 3 अलग नतीजे मिले — कभी सही entities, कभी बिलकुल खाली):

```python
# Call A — reasoning खुला छोड़ा गया, पर सख़्त JSON फ़ॉर्मैट नहीं माँगा —
# इसलिए यह ख़ुद को जवाब देने की जगह से वंचित नहीं कर सकता
analysis = _call_sarvam(analysis_prompt, api_key, _MODEL_FAST)

# Call B — Call A के जवाब को सख़्त JSON में ट्रांसक्राइब करता है, reasoning बंद
raw = _call_sarvam(
    extraction_prompt, api_key, _MODEL_FAST,
    system="/no_think Output JSON only.",
    reasoning_effort=None,   # पूरा बजट लिखने के लिए, सोचने के लिए नहीं
)
# → {"skip": false, "search_queries": [...], "entities": [{"name": "...", "type": "..."}]}
```

महँगा मॉडल (`sarvam-105b`) कभी भी ऐसा आर्टिकल नहीं देखता जो पब्लिश होने लायक़ नहीं है — वह तभी बुलाया जाता है जब Stage 1 पहले ही तय कर चुका होता है कि स्टोरी और उसकी एंटिटीज़ लिखे जाने लायक़ हैं।

GitHub से आने वाले repos के लिए एक और लेयर है, Stage 1 से भी पहले: `collectors/github_collector.py` में एक सस्ता, deterministic फ़िल्टर (job-listing keywords, "Ultimate...Guide 2026" जैसे listicle पैटर्न, हफ़्ते भर पुराने repo के लिए 20-star फ़्लोर) हर चीज़ को रोकता है जो किसी LLM कॉल के लायक़ ही नहीं है, और उसके बाद `writer/github_gate.py` में एक बैच्ड एडिटोरियल जज कॉल (`sarvam-105b`) बचे हुए repos में से सिर्फ़ genuine नए AI/ML developments को आगे जाने देता है — guides, "awesome-lists", और off-topic repos को नहीं।

### कॉन्टेक्स्ट लेयर — यही इसे सिर्फ़ "AI-written" नहीं बल्कि "context-aware" बनाता है

Stage 1 जो भी एंटिटी ढूँढता है, उसे किसी सर्च API तक पहुँचने से पहले एक **persistent, 45-दिन-TTL नॉलेज कैश** के ख़िलाफ़ चेक किया जाता है:

```python
# writer/entity_cache.py
def get_entity(cache: dict, name: str, resolved_sense: str | None = None) -> dict | None:
    record = cache.get(_normalize(name))
    if "senses" in record:              # ambiguous entity — needs a sense match
        for sense in record["senses"]:
            if sense.get("sense_label") == resolved_sense and _is_fresh(sense):
                return sense
        return None
    return record if _is_fresh(record) else None
```

दो चीज़ें जो एक सीधा-सादा ट्रांसलेटर कभी नहीं कर सकता, लेकिन यह कैश करता है:
- **रन्स के बीच मेमोरी।** आज के आर्टिकल में जिस एंटिटी का ज़िक्र है, उस पर शायद किसी पिछले रन में पहले ही रिसर्च हो चुकी है — cache hit, कोई नई API कॉल नहीं, और आर्टिकल उस एंटिटी को सिर्फ़ नाम से नहीं बल्कि असली बैकग्राउंड के साथ रेफ़र कर सकता है।
- **डिसएंबिग्युएशन (Disambiguation)।** अगर "Claude" (AI मॉडल) और किसी और अर्थ वाला "Claude" कभी टकराएँ, तो Stage 1 का `resolved_sense` उन्हें अलग-अलग cached senses के तौर पर रखता है, न कि दो असंबंधित कॉन्टेक्स्ट को चुपचाप एक ही समरी में मिलाकर।

Cache miss होने पर, सर्च दो तरह की queries को अलग-अलग tier chain से गुज़ारता है — क्योंकि "यह entity क्या है" और "यह अभी क्यों relevant है" के लिए अलग तरह के source चाहिए, और दोनों बिना किसी पेड API key के, मुफ़्त में काम करते हैं:

```python
# writer/search.py
IDENTITY_TIERS = [_ddg_search]                        # "what is X"
CONTEXT_TIERS = [_google_news_rss, _ddg_search]        # "why now" — needs recent material

def search_web(queries: list[str], tiers: list) -> dict[str, str]:
    for q in queries:
        for tier in tiers:
            text = tier(q)
            if text:
                break
        results[q] = text
    return results
```

पहले यहाँ Tavily और Exa जैसी paid tiers पहले नंबर पर थीं — लेकिन असल में Exa की कभी key ही configure नहीं हुई थी, और Tavily की key revoke हो चुकी थी, तो हर query चुपचाप सीधे आख़िरी DDG tier पर गिर रही थी। इसी session में दोनों paid tiers हटा दी गईं और उनकी जगह दो मुफ़्त, key-रहित sources आए — Wikipedia (identity queries के लिए) और Google News RSS (context/why-now queries के लिए, इसी repo में पहले से इस्तेमाल हो रही `feedparser` लाइब्रेरी के ज़रिए)।

दोनों नए sources को लाइव टेस्ट करने पर असली दिक़्क़तें मिलीं, थ्योरी में नहीं:
- Wikipedia का सर्च बिना किसी relevance floor के चलता है — "Bolt Graphics" (एक real GPU startup) की क्वेरी "Rock n' Bolt" नाम के एक अनसंबंधित 1984 वीडियो गेम से मैच हो गई।
- Google News RSS के लिंक असल में Google के redirect-wrapper URLs होते हैं जो client-side JS से resolve होते हैं — उन्हें सीधे scrape करने पर असली आर्टिकल की जगह Google का cookie-consent पेज मिलता रहा। Fix: लिंक scrape करने के बजाय सीधे RSS entry की headline इस्तेमाल की गई, जो अकेले भी काफ़ी informative निकलीं।

पहली दिक़्क़त की वजह से Wikipedia को इसी session में पूरी तरह हटा दिया गया — यह सिर्फ़ एक relevance-floor bug की बात नहीं थी, बल्कि साफ़ निर्देश था कि Wikipedia को किसी भी रूप में source नहीं माना जाना चाहिए (बहुत unreliable/biased)। सिर्फ़ dedicated Wikipedia tier हटाना काफ़ी नहीं था — DDG का अपना organic ranking भी कभी-कभी Wikipedia का पेज ही टॉप रिज़ल्ट के तौर पर लौटा देता है (लाइव टेस्ट में एक "Rocket Lab" क्वेरी का जवाब Wikipedia-स्टाइल citation markers जैसे `[15]` के साथ आया), तो अब `_ddg_search` खुद भी `wikipedia.org`/`wikimedia.org`/`wiktionary.org`/`wikidata.org` डोमेन के नतीजों को पूरी तरह छोड़ देता है, चाहे वे कितने भी relevant क्यों न लगें।

आख़िरी DDG tier (`_ddg_search`) पहले असल में टूटा हुआ निकला था, सिर्फ़ थ्योरी में नहीं — लाइव टेस्ट में 6 असली queries में से 5 खाली आईं, और एक ने "Zeus GPU" (Bolt Graphics का प्रोडक्ट) को ग्रीक पौराणिक कथाओं के ज़्यूस से मिला दिया। वजह: डिपेंडेंसी deprecated `duckduckgo_search` पैकेज इस्तेमाल कर रही थी। नए `ddgs` पैकेज पर स्विच करने के बाद वही 6 queries 6/6 सही और relevant नतीजे लेकर आईं — कोई SearXNG जैसा भारी fix नहीं चाहिए था, सिर्फ़ एक डिपेंडेंसी अपडेट।

सर्च से जो raw material मिलता है, उसे सीधे `entity_context` में डालने के बजाय अब एक और छोटा `sarvam-30b` कॉल (`reasoning_effort=None`) उसे साफ़, सीधे जवाब में बदल देता है — पहले Stage 2 को raw snippets के ढेर में से ख़ुद अंदाज़ा लगाना पड़ता था कि असली जवाब कौन-सा हिस्सा है।

### Stage 2 — मॉडल प्लान बनाता है; अभी लिखता नहीं

```python
paragraph_plan rules:
- 3-4 paragraphs total
- Each instruction must be specific enough that the writer needs zero additional thinking
- Include which facts/entities/numbers belong in that paragraph
```

यह विभाजन एक hard constraint की वजह से है: Sarvam के starter plan पर **`max_tokens: 4096`**, और दोनों मॉडल लिखने से पहले reason करते हैं — जिससे reasoning चुपचाप वह टोकन बजट खा जाता था जो आर्टिकल के टेक्स्ट के लिए बचना चाहिए था। "क्या कहना है" (Stage 2) को "इसे कहो" (Stage 3) से अलग करने का मतलब है कि लिखने वाला कॉल reasoning को लगभग पूरी तरह छोड़ सकता है:

```python
# Stage 3 call
raw = _call_sarvam(
    prompt, api_key, _MODEL_QUALITY,
    system="/no_think Output JSON only. No preamble.",
    reasoning_effort=None,
)
```

यह एक टोकन-बजट प्रॉब्लम है जिसे पाइपलाइन को फिर से डिज़ाइन करके सुलझाया गया — बड़ी token limit से नहीं (जो इस प्लान पर उपलब्ध ही नहीं है)। एक दिलचस्प डिटेल: सिर्फ़ सिस्टम प्रॉम्प्ट में `/no_think` लिखना कुछ नहीं करता — यह सिर्फ़ टेक्स्ट है जिसे मॉडल मान भी सकता है, नहीं भी (टेस्ट में यह साबित हुआ: बिना `reasoning_effort=None` के वही प्रॉम्प्ट फिर भी 3000+ टोकन reasoning पर ख़र्च कर देता था)। असल कंट्रोल सिर्फ़ `reasoning_effort` पैरामीटर है।

आउटपुट फ़ॉर्मैट भी इसी वजह से labeled text से JSON में बदला गया — असली आर्टिकल्स पर मॉडल कभी-कभी `TITLE:` की जगह हिंदी लेबल (`शीर्षक:`) या मार्कडाउन-रैप्ड लेबल (`**शीर्षक:**`) लिख देता था, जिसे पुराना strict parser पहचान नहीं पाता था — और एक बार तो पूरा `strategic_analysis` सेक्शन ग़ायब हो गया क्योंकि उसका कंटेंट बिना पहचाने गए लेबल की वजह से पिछले सेक्शन में मिल गया। JSON वही robust parser इस्तेमाल करता है जो Stage 1/2 पहले से इस्तेमाल करते हैं।

### कैटेगरी-अवेयर संपादकीय आवाज़, हर चीज़ के लिए एक जैसा टोन नहीं

```python
_CATEGORY_FRAMING = {
    "acquisition": "frame as probable/possible market impact — always hedge with "
                   "हो सकता है / संभावना है, never state predictions as fact",
    "model_release": "translate benchmark numbers into what they mean in practice, "
                      "not just what the number is",
    "ban_regulation": "separate immediate impact from broader, more speculative "
                       "implications; hedge the latter explicitly",
    "repo_analysis": "explain real-world developer/industry impact using a simple "
                      "analogy for the core technical mechanism",
}
```

Stage 2 स्टोरी को इनमें से किसी एक कैटेगरी में क्लासिफ़ाई करता है; Stage 3 उसी फ़्रेमिंग के तहत आख़िरी पैराग्राफ़ लिखता है। एक अधिग्रहण (acquisition) की अफ़वाह और एक बेंचमार्क रिलीज़ को structurally अलग तरीक़े से ट्रीट किया जाता है — बिल्कुल वैसे ही जैसे कोई असली एडिटोरियल डेस्क करेगा।

### प्रोडक्शन में असल में देखी गई ख़राबियों को ठीक करने वाला पोस्ट-प्रोसेसिंग

```python
def _is_meta_line(line: str) -> bool:
    """True if a line looks like model self-commentary ('Let me...', 'Here's...'),
    not article text."""

def _trim_to_last_sentence(text: str) -> str:
    """If generation got cut off mid-sentence by the token cap, trim back
    to the last complete sentence instead of publishing a fragment."""
```

ये दोनों फ़ंक्शन इसलिए बने क्योंकि `pipeline_trace.json` (हर आर्टिकल की, हर स्टेज के raw आउटपुट की एंट्री) में ये फेलियर पैटर्न बड़े पैमाने पर दिखाई दिए — पहले से अंदाज़ा लगाकर नहीं जोड़े गए।

### साथ-साथ तुलना

| | `translator/translate.py` (जेनेरिक) | `writer/synthesize.py` (context-aware) |
|---|---|---|
| इनपुट | सिर्फ़ एक आर्टिकल का टेक्स्ट, अकेला | आर्टिकल + persistent एंटिटी मेमोरी + लाइव सर्च रिज़ल्ट्स |
| रन्स के बीच मेमोरी | कुछ नहीं — हर बार stateless | हर आर्टिकल के लिए साझा 45-दिन का एंटिटी कैश |
| आवाज़/टोन | हर स्टोरी के लिए एक जैसा | कैटेगरी-विशेष फ़्रेमिंग (हेज्ड, बेंचमार्क-ट्रांसलेटेड, एनालॉजी-ड्रिवन...) |
| मॉडल खर्च | 1 ट्रांसलेशन कॉल | पहले सस्ता मॉडल triage करता है, महँगा मॉडल सिर्फ़ अप्रूव्ड स्टोरीज़ पर चलता है |
| फ़ेलियर विज़िबिलिटी | try/except, best-effort | हर स्टेज का पूरा per-article ट्रेस (`pipeline_trace.json`) |

---

## 🎨 डिज़ाइन भाषा (Aesthetic)

**थीम: "Temple × Medium.org"** — एक टिपिकल भीड़-भाड़ वाली न्यूज़ साइट नहीं, बल्कि Medium जैसा शांत, प्रीमियम पढ़ने का अनुभव।

| एलिमेंट | विवरण |
|---|---|
| **रंग और टेक्सचर** | सैंडस्टोन (Sandstone), गेरू/टेराकोटा (Temple Red), पीतल (Aged Brass) — प्राचीन भारतीय कला से प्रेरित। बैकग्राउंड में हल्का पेपर/स्टोन noise टेक्सचर। |
| **टाइपोग्राफ़ी** | हिंदी टेक्स्ट के लिए **Martel (Serif)** फॉन्ट — एक प्रीमियम जर्नल जैसा फील। |
| **सिग्नेचर एलिमेंट** | राइट-बॉटम कॉर्नर में **कोणार्क का सूर्य चक्र** — स्क्रॉल करने पर घूमता है, समय और भारतीय वैज्ञानिक धरोहर का प्रतीक। |

### AI Integrations (UI में)

- **🔹 त्वरित सारांश (AI Summary)** — एक क्लिक में पूरे आर्टिकल का 3-बुलेट-पॉइंट सारांश
- **🔹 जिज्ञासा (Ask AI)** — इन-बिल्ट चैट मॉडल जहाँ पाठक उस आर्टिकल से जुड़े वैज्ञानिक सवाल हिंदी में पूछ सकता है (Gemini API से इंटीग्रेटेड)

---

## ⚙️ सेटअप (Setup)

```bash
python -m venv .venv
.venv\Scripts\activate
pip install -r requirements.txt
cp .env.example .env   # GitHub token, LLM/Gemini API keys भरें
```

### चलाना (Run)

```bash
# पूरी पाइपलाइन — collection + clustering + Sarvam AI writing — एक ही कमांड में
# (आमतौर पर GitHub Actions cron से रोज़ अपने-आप चलता है)
python pipeline.py
```

### कॉन्फ़िगरेशन

- `config/feeds.yaml` — RSS feed स्रोतों की सूची
- `config/github.yaml` — GitHub trending/search queries
- `.env` — सीक्रेट्स (GitHub token, Gemini/LLM API keys)

---

## 📌 स्टेटस

🟡 **Phase 1 शुरुआती चरण में** — AI/कंप्यूटर न्यूज़ कलेक्शन और हिंदी अनुवाद पाइपलाइन पर काम जारी। गेम डेवलपमेंट (Unity/Unreal/VR) और स्पेस सेक्शन आगे के फ़ेज़ में जोड़े जाएंगे।

---

## 🙏 क्रेडिट

स्रोत लेखों (TechCrunch, Space.com, NASA, आदि) के तथ्यों पर आधारित मूल हिंदी लेखन। हर आर्टिकल में मूल स्रोत का लिंक/क्रेडिट देना इस प्रोजेक्ट की पारदर्शिता और भरोसे की नींव है।
