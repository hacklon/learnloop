"""
LearnLoop — an AI study buddy that remembers what you learned, so you don't forget it.
Neo4j holds a context graph with all three memory layers:
  1. Short-term  : every conversation and message, chained in order
  2. Long-term   : concepts, topics, prerequisites, memory strength (forgetting curve), memory palace
  3. Reasoning   : why the agent chose each revision strategy, and whether it worked

Setup:
  pip install neo4j openai anthropic
  set NEO4J_URI, NEO4J_USER, NEO4J_PASSWORD
  and OPENAI_API_KEY or ANTHROPIC_API_KEY (OpenAI is used if both are set)

Run:
  python learnloop_agent.py      (choose teacher or student at the start)
Teacher mode:
  type what you taught today -> saved as a Lesson for the class
  notes    -> paste full class notes (end with END), or 'notes file.txt' to load a text file;
              notes are split per concept and the student agent teaches and quizzes from them
  plan     -> notes for TOMORROW's class (or 'plan file.txt'); students get a quick preview,
              linked to what they already know
  report   -> which concepts the class is forgetting, and who has not revised
  chapters -> list chapters and their status
  finish X -> mark chapter X finished; every student gets a chapter test
  subject  -> chapter-by-chapter class test results, students at risk, subject completion
  reviews  -> every 2-week review per student, and class-wide weak spots
Student commands:
  tomorrow -> quick preview of the next class (or just ask "what's in tomorrow's class?")
  test     -> take / continue the chapter test for a finished chapter
  revise   -> what to revise and WHY, computed from all your answers this term
  review   -> take / continue the 2-week cumulative review (due every 14 days, whole term so far)
  progress -> chapter scores, memory per chapter, week-by-week results, subject completion
  skip 3   -> pretend 3 days have passed (to demo forgetting)
  status   -> memory strength of every concept
  palace   -> the student's memory palace (method of loci)
  why      -> the agent's recent decisions, reasons and outcomes
  stats    -> which revision strategies work best for this student
  quit

Graph model:
  Short-term : (Student)-[:HAS_CONVERSATION]->(Conversation)-[:FIRST_MESSAGE]->(Message)-[:NEXT_MESSAGE]->(Message)
               (Message)-[:MENTIONS]->(Concept)
  Long-term  : (Student)-[:KNOWS {stability, last_reviewed, reviews, correct}]->(Concept)
               (Concept)-[:PART_OF]->(Topic), (Concept)-[:PREREQUISITE_OF]->(Concept)
               (Student)-[:HAS_PALACE]->(Palace)-[:HAS_LOCUS {order}]->(Locus)<-[:PLACED_AT {image}]-(Concept)
  Reasoning  : (Message)-[:TRIGGERED]->(ReasoningTrace {strategy, reason, success})-[:TARGETED]->(Concept)
               (Student)-[:HAS_TRACE]->(ReasoningTrace)
"""
import os, json, math, time, uuid
from neo4j import GraphDatabase

try:  # optional: load keys from a .env file next to this script
    from dotenv import load_dotenv
    load_dotenv(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env"))
except ImportError:
    pass

# ---------- LLM (OpenAI if key present, else Anthropic) ----------
if os.getenv("OPENAI_API_KEY"):
    from openai import OpenAI
    _oa = OpenAI()
    def llm(system, user):
        r = _oa.chat.completions.create(
            model=os.getenv("LLM_MODEL", "gpt-4o-mini"),
            response_format={"type": "json_object"},
            messages=[{"role": "system", "content": system},
                      {"role": "user", "content": user}])
        return r.choices[0].message.content
else:
    from anthropic import Anthropic
    _an = Anthropic()
    def llm(system, user):
        r = _an.messages.create(model=os.getenv("LLM_MODEL", "claude-sonnet-5"),
                                max_tokens=4000, system=system,
                                messages=[{"role": "user", "content": user}])
        t = "".join(b.text for b in r.content if getattr(b, "type", "") == "text")
        return t[t.find("{"): t.rfind("}") + 1]

driver = GraphDatabase.driver(os.environ["NEO4J_URI"],
                              auth=(os.environ["NEO4J_USER"], os.environ["NEO4J_PASSWORD"]),
                              notifications_min_severity="OFF")  # hide harmless label warnings
DAY = 86400
day_offset = 0          # demo time travel
conv_id = None          # current conversation
last_msg_id = None      # tail of the message chain

def now():
    return time.time() + day_offset * DAY

def new_id():
    return uuid.uuid4().hex[:12]

def retention(stability_days, last_reviewed):
    """Forgetting curve: R = e^(-t/S). 1.0 = fresh, below 0.7 = needs revision."""
    days = max(0.0, (now() - last_reviewed) / DAY)
    return math.exp(-days / max(stability_days, 0.1))

# =====================================================================
# 1. SHORT-TERM MEMORY — conversations and messages
# =====================================================================
def start_conversation(student):
    global conv_id, last_msg_id
    conv_id, last_msg_id = new_id(), None
    with driver.session() as s:
        s.run("MERGE (st:Student {name:$s}) "
              "CREATE (st)-[:HAS_CONVERSATION]->(:Conversation {id:$c, started:$t})",
              s=student, c=conv_id, t=now())

def add_message(role, text):
    """Append a message to the current conversation chain; returns its id."""
    global last_msg_id
    mid = new_id()
    with driver.session() as s:
        if last_msg_id is None:
            s.run("MATCH (c:Conversation {id:$c}) "
                  "CREATE (c)-[:FIRST_MESSAGE]->(:Message {id:$m, role:$r, text:$x, ts:$t})",
                  c=conv_id, m=mid, r=role, x=text, t=now())
        else:
            s.run("MATCH (p:Message {id:$p}) "
                  "CREATE (p)-[:NEXT_MESSAGE]->(:Message {id:$m, role:$r, text:$x, ts:$t})",
                  p=last_msg_id, m=mid, r=role, x=text, t=now())
    last_msg_id = mid
    return mid

def recent_messages(student, n=8):
    q = """
    MATCH (:Student {name:$s})-[:HAS_CONVERSATION]->(:Conversation)-[:FIRST_MESSAGE|NEXT_MESSAGE*]->(m:Message)
    RETURN m.role AS role, m.text AS text, m.ts AS ts ORDER BY m.ts DESC LIMIT $n
    """
    with driver.session() as s:
        rows = s.run(q, s=student, n=n).data()
    return [f'{r["role"]} ({round((now() - r["ts"]) / DAY, 1)} days ago): {r["text"][:600]}'
            for r in reversed(rows)]

# =====================================================================
# 2. LONG-TERM MEMORY — concepts, forgetting curve, memory palace
# =====================================================================
def get_knowledge(student):
    q = """
    MATCH (s:Student {name:$s})-[k:KNOWS]->(c:Concept)
    OPTIONAL MATCH (c)-[:PART_OF]->(t:Topic)
    OPTIONAL MATCH (p:Concept)-[:PREREQUISITE_OF]->(c)
    OPTIONAL MATCH (s)-[pk:KNOWS]->(p)
    RETURN c.name AS concept, t.name AS topic, k.stability AS stability,
           k.last_reviewed AS last, k.reviews AS reviews, k.correct AS correct,
           collect(DISTINCT {name:p.name, stability:pk.stability, last:pk.last_reviewed}) AS prereqs
    """
    with driver.session() as s:
        rows = s.run(q, s=student).data()
    mem = []
    for r in rows:
        R = retention(r["stability"], r["last"])
        weak_prereqs = [p["name"] for p in r["prereqs"] if p["name"] and
                        (p["stability"] is None or retention(p["stability"], p["last"]) < 0.7)]
        mem.append({"concept": r["concept"], "topic": r["topic"],
                    "memory_strength": round(R, 2),
                    "status": "fresh" if R >= 0.85 else "fading" if R >= 0.7 else "FORGETTING",
                    "days_since_review": round((now() - r["last"]) / DAY, 1),
                    "quiz_score": f'{r["correct"]}/{r["reviews"]}',
                    "weak_prerequisites": weak_prereqs})
    mem.sort(key=lambda m: m["memory_strength"])
    palace = get_palace(student)
    placed = {p["concept"]: p for p in palace if p["concept"]}
    for m in mem:
        if m["concept"] in placed:
            m["palace_cue"] = {"locus": placed[m["concept"]]["locus"],
                               "image": placed[m["concept"]]["image"]}
    return mem, palace

def get_palace(student):
    """Method of loci: the student's familiar places, in walking order, and what is stored at each."""
    q = """
    MATCH (:Student {name:$s})-[:HAS_PALACE]->(pal:Palace)-[h:HAS_LOCUS]->(l:Locus)
    OPTIONAL MATCH (c:Concept)-[pl:PLACED_AT]->(l)
    RETURN pal.name AS palace, h.order AS order, l.name AS locus,
           c.name AS concept, pl.image AS image
    ORDER BY h.order
    """
    with driver.session() as s:
        return s.run(q, s=student).data()

# =====================================================================
# 3. REASONING MEMORY — decisions, reasons, outcomes
# =====================================================================
def strategy_stats(student):
    q = """
    MATCH (:Student {name:$s})-[:HAS_TRACE]->(t:ReasoningTrace)
    WHERE t.success IS NOT NULL
    RETURN t.strategy AS strategy, count(t) AS tries,
           sum(CASE WHEN t.success THEN 1 ELSE 0 END) AS worked
    ORDER BY tries DESC
    """
    with driver.session() as s:
        rows = s.run(q, s=student).data()
    return [{**r, "success_rate": round(r["worked"] / r["tries"], 2)} for r in rows]

def get_open_question(student):
    """The quiz question the agent asked last and is waiting for an answer to (stored in the graph)."""
    q = """
    MATCH (:Student {name:$s})-[:HAS_TRACE]->(t:ReasoningTrace)
    WHERE t.success IS NULL AND t.skipped IS NULL AND t.question IS NOT NULL
    OPTIONAL MATCH (t)-[:TARGETED]->(c:Concept)
    RETURN t.question AS question, t.expected_answer AS expected_answer,
           t.strategy AS strategy, t.chapter_id AS chapter_id, t.review_id AS review_id,
           collect(c.name) AS concepts, t.ts AS ts
    ORDER BY ts DESC LIMIT 1
    """
    with driver.session() as s:
        return s.run(q, s=student).single(strict=False)

# =====================================================================
# SUBJECTS, CHAPTERS, TESTS — long-term progress over weeks
# =====================================================================
def get_chapter_tests_due(student):
    """Finished chapters of the student's class, and which concepts still need a chapter-test answer."""
    q = """
    MATCH (s:Student {name:$s})-[:ENROLLED_IN]->(:Class)-[:STUDIES]->(sub:Subject)
          -[:HAS_CHAPTER]->(ch:Chapter {status:'finished'})<-[:PART_OF_CHAPTER]-(:Lesson)-[:INCLUDES]->(c:Concept)
    OPTIONAL MATCH (s)-[:ATTEMPTED]->(a:Attempt {kind:'chapter_test'})-[:FOR_CHAPTER]->(ch)
    OPTIONAL MATCH (a)-[:ON]->(ac:Concept) WHERE ac = c
    WITH sub, ch, c, collect(ac) AS tested
    WITH sub, ch, collect({concept:c.name, tested: size(tested) > 0}) AS concepts
    RETURN sub.name AS subject, ch.name AS chapter, ch.id AS chapter_id, concepts
    """
    with driver.session() as s:
        rows = s.run(q, s=student).data()
    due = []
    for r in rows:
        todo = [c["concept"] for c in r["concepts"] if not c["tested"]]
        if todo:
            due.append({"subject": r["subject"], "chapter": r["chapter"], "chapter_id": r["chapter_id"],
                        "concepts_left_to_test": todo, "total_concepts": len(r["concepts"])})
    return due

def get_progress(student):
    """Per subject and chapter: status, current memory, chapter-test score. Plus weekly history."""
    q = """
    MATCH (s:Student {name:$s})-[:ENROLLED_IN]->(:Class)-[:STUDIES]->(sub:Subject)-[:HAS_CHAPTER]->(ch:Chapter)
          <-[:PART_OF_CHAPTER]-(l:Lesson)-[:INCLUDES]->(c:Concept)
    WHERE l.ts <= $now
    OPTIONAL MATCH (s)-[k:KNOWS]->(c)
    RETURN DISTINCT sub.name AS subject, ch.id AS chapter_id, ch.name AS chapter, ch.status AS status,
           ch.started AS started, c.name AS concept, k.stability AS stability, k.last_reviewed AS last
    """
    tests_q = """
    MATCH (:Student {name:$s})-[:ATTEMPTED]->(a:Attempt {kind:'chapter_test'})-[:FOR_CHAPTER]->(ch:Chapter)
    RETURN ch.id AS chapter_id, count(a) AS answered, sum(CASE WHEN a.correct THEN 1 ELSE 0 END) AS correct
    """
    hist_q = """
    MATCH (:Student {name:$s})-[:ATTEMPTED]->(a:Attempt)
    RETURN a.ts AS ts, a.correct AS correct, a.kind AS kind ORDER BY ts
    """
    with driver.session() as s:
        rows = s.run(q, s=student, now=now()).data()
        tests = {r["chapter_id"]: r for r in s.run(tests_q, s=student).data()}
        hist = s.run(hist_q, s=student).data()
    subjects = {}
    for r in rows:
        sub = subjects.setdefault(r["subject"], {})
        ch = sub.setdefault(r["chapter_id"], {"chapter": r["chapter"], "status": r["status"],
                                              "started": r["started"], "memory": [], "concepts": 0})
        ch["concepts"] += 1
        ch["memory"].append(retention(r["stability"], r["last"]) if r["stability"] is not None else 0.0)
    out = {}
    for sub_name, chapters in subjects.items():
        chs = []
        for cid, ch in sorted(chapters.items(), key=lambda kv: kv[1]["started"] or 0):
            t = tests.get(cid)
            chs.append({"chapter": ch["chapter"], "status": ch["status"], "concepts": ch["concepts"],
                        "avg_memory": round(sum(ch["memory"]) / len(ch["memory"]), 2),
                        "chapter_test": f'{t["correct"]}/{t["answered"]}' if t else "not taken"})
        out[sub_name] = {"chapters": chs,
                         "subject_complete": all(c["status"] == "finished" for c in chs)}
    weeks = {}
    if hist:
        start = hist[0]["ts"]
        for h in hist:
            w = int((h["ts"] - start) // (7 * DAY)) + 1
            d = weeks.setdefault(w, [0, 0])
            d[0] += 1 if h["correct"] else 0
            d[1] += 1
    return {"subjects": out,
            "weekly_results": [{"week": w, "correct": c, "answered": n} for w, (c, n) in sorted(weeks.items())]}

def recent_traces(student, n=5):
    q = """
    MATCH (:Student {name:$s})-[:HAS_TRACE]->(t:ReasoningTrace)
    OPTIONAL MATCH (t)-[:TARGETED]->(c:Concept)
    RETURN t.strategy AS strategy, t.reason AS reason, t.success AS success,
           t.skipped AS skipped, t.question AS question,
           t.ts AS ts, collect(c.name) AS concepts
    ORDER BY ts DESC LIMIT $n
    """
    with driver.session() as s:
        return s.run(q, s=student, n=n).data()

# =====================================================================
# FORTNIGHTLY REVIEW — every 14 days, a cumulative revision test over everything covered so far,
# prioritised by this student's own answer history (the Attempt memory)
# =====================================================================
REVIEW_EVERY_DAYS = 14
REVIEW_QUESTIONS = 6

def notes_for(student, concepts, per_concept=2):
    """Teacher's notes for these concepts from ANY lesson of the student's class (any week of the term)."""
    if not concepts:
        return {}
    q = """
    MATCH (:Student {name:$s})-[:ENROLLED_IN]->(:Class)-[:HAD_LESSON]->(l:Lesson)-[:HAS_NOTE]->(n:Note)
          -[:EXPLAINS]->(c:Concept)
    WHERE c.name IN $names AND l.ts <= $now
    RETURN c.name AS concept, collect(n.text)[..$k] AS notes
    """
    with driver.session() as s:
        return {r["concept"]: r["notes"] for r in
                s.run(q, s=student, names=[c.lower() for c in concepts], now=now(), k=per_concept).data()}

def revision_priorities(student):
    """Every concept covered in class so far, scored by how much this student needs to revise it."""
    q = """
    MATCH (s:Student {name:$s})-[:ENROLLED_IN]->(:Class)-[:HAD_LESSON]->(l:Lesson)-[:INCLUDES]->(c:Concept)
    WHERE l.ts <= $now
    WITH s, c, min(l.ts) AS taught
    OPTIONAL MATCH (s)-[k:KNOWS]->(c)
    OPTIONAL MATCH (s)-[:ATTEMPTED]->(a:Attempt)-[:ON]->(c)
    WITH c, taught, k, a ORDER BY a.ts
    RETURN c.name AS concept, taught, k.stability AS stability, k.last_reviewed AS last,
           collect(CASE WHEN a IS NULL THEN NULL ELSE {ts:a.ts, correct:a.correct} END) AS attempts
    """
    with driver.session() as s:
        rows = s.run(q, s=student, now=now()).data()
    out = []
    for r in rows:
        atts = r["attempts"]
        total = len(atts)
        wrong = sum(1 for a in atts if not a["correct"])
        R = retention(r["stability"], r["last"]) if r["stability"] is not None else 0.0
        last_wrong = bool(atts) and not atts[-1]["correct"]
        score = (1 - R) * 2 + (wrong / total if total else 0.5) * 2 + (1.5 if last_wrong else 0) + (1 if not total else 0)
        why = []
        if not total:
            why.append("never answered")
        if last_wrong:
            why.append("last answer was wrong")
        if wrong:
            why.append(f"{wrong}/{total} answers wrong")
        why.append(f"memory {R:.2f}")
        out.append({"concept": r["concept"], "priority": round(score, 2), "why": ", ".join(why),
                    "taught_days_ago": round((now() - r["taught"]) / DAY, 1)})
    out.sort(key=lambda x: -x["priority"])
    return out

def get_fortnight_review(student):
    """Which 2-week review cycle we are in, and what it should test (top-priority concepts not yet answered)."""
    with driver.session() as s:
        start = s.run("""
        MATCH (:Student {name:$s})-[:ENROLLED_IN]->(:Class)-[:HAD_LESSON]->(l:Lesson) WHERE l.ts <= $now
        RETURN min(l.ts) AS start""", s=student, now=now()).single()["start"]
    if start is None:
        return None
    cycle = int((now() - start) // (REVIEW_EVERY_DAYS * DAY))
    next_in = round((start + (cycle + 1) * REVIEW_EVERY_DAYS * DAY - now()) / DAY, 1)
    if cycle < 1:
        return {"due": False, "next_review_in_days": next_in}
    rid = f"{student}|{cycle}"
    with driver.session() as s:
        answered = s.run("""
        MATCH (:Student {name:$s})-[:ATTEMPTED]->(a:Attempt {kind:'fortnight_review'})-[:FOR_REVIEW]->(:Review {id:$rid}),
              (a)-[:ON]->(c:Concept)
        RETURN c.name AS concept, a.correct AS correct""", s=student, rid=rid).data()
    done = {a["concept"] for a in answered}
    left = max(0, REVIEW_QUESTIONS - len(answered))
    to_test = [p for p in revision_priorities(student) if p["concept"] not in done][:left]
    notes = notes_for(student, [p["concept"] for p in to_test])
    for p in to_test:
        p["teacher_notes"] = notes.get(p["concept"], [])
    return {"due": left > 0, "review_id": rid, "cycle": cycle,
            "covers": f"everything taught in the last {cycle * REVIEW_EVERY_DAYS} days",
            "answered_so_far": answered,
            "score_so_far": f'{sum(1 for a in answered if a["correct"])}/{len(answered)}',
            "concepts_to_test_next": to_test, "next_review_in_days": next_in}

# ---------- full context for the LLM ----------
def get_class_lessons(student):
    """Lessons the teacher logged for the student's class, and which concepts this student has revised."""
    q = """
    MATCH (s:Student {name:$s})-[:ENROLLED_IN]->(cl:Class)-[:HAD_LESSON]->(l:Lesson)-[:INCLUDES]->(c:Concept)
    WHERE l.ts <= $now
    OPTIONAL MATCH (s)-[k:KNOWS]->(c)
    OPTIONAL MATCH (l)-[:HAS_NOTE]->(n:Note)-[:EXPLAINS]->(c)
    WITH cl, l, c, k, collect(n.text) AS notes
    WITH cl, l, collect({concept:c.name, revised_by_student: k IS NOT NULL,
                         teacher_notes: notes}) AS concepts
    RETURN cl.name AS class, l.subject AS subject, l.summary AS summary, l.ts AS ts, concepts
    ORDER BY ts DESC LIMIT 5
    """
    with driver.session() as s:
        rows = s.run(q, s=student, now=now()).data()
    for r in rows:
        r["taught"] = f'{round((now() - r.pop("ts")) / DAY, 1)} days ago'
    return rows

def get_upcoming_lessons(student):
    """Lessons the teacher planned for the student's class (scheduled in the future), with each
    concept's prerequisites and how well THIS student remembers them."""
    q = """
    MATCH (s:Student {name:$s})-[:ENROLLED_IN]->(cl:Class)-[:HAD_LESSON]->(l:Lesson)-[:INCLUDES]->(c:Concept)
    WHERE l.ts > $now
    OPTIONAL MATCH (l)-[:HAS_NOTE]->(n:Note)-[:EXPLAINS]->(c)
    OPTIONAL MATCH (p:Concept)-[:PREREQUISITE_OF]->(c)
    OPTIONAL MATCH (s)-[pk:KNOWS]->(p)
    WITH l, c, collect(DISTINCT n.text) AS notes,
         collect(DISTINCT {name:p.name, stability:pk.stability, last:pk.last_reviewed}) AS prereqs
    WITH l, collect({concept:c.name, teacher_notes:notes, prereqs:prereqs}) AS concepts
    RETURN l.subject AS subject, l.summary AS summary, l.ts AS ts, concepts
    ORDER BY ts ASC LIMIT 3
    """
    with driver.session() as s:
        rows = s.run(q, s=student, now=now()).data()
    for r in rows:
        r["scheduled"] = f'in {round((r.pop("ts") - now()) / DAY, 1)} days'
        for c in r["concepts"]:
            builds_on = []
            for p in c.pop("prereqs"):
                if not p["name"]:
                    continue
                if p["stability"] is None:
                    status = "not studied yet"
                else:
                    R = retention(p["stability"], p["last"])
                    status = f'memory {R:.2f} ' + ("(needs revision)" if R < 0.7 else "(ok)")
                builds_on.append({"concept": p["name"], "student_status": status})
            c["builds_on"] = builds_on
    return rows

def get_context(student):
    knowledge, palace = get_knowledge(student)
    # teacher's notes for the 5 weakest concepts, from any week of the term (not just recent lessons)
    weak_notes = notes_for(student, [k["concept"] for k in knowledge[:5]])
    for k in knowledge[:5]:
        if weak_notes.get(k["concept"]):
            k["teacher_notes"] = weak_notes[k["concept"]]
    return {
        "fortnight_review": get_fortnight_review(student),
        "short_term_recent_messages": recent_messages(student),
        "class_lessons_from_teacher": get_class_lessons(student),
        "upcoming_class_preview": get_upcoming_lessons(student),
        "long_term_concepts_weakest_first": knowledge,
        "memory_palace": palace,
        "free_loci": [p["locus"] for p in palace if not p["concept"]],
        "reasoning_strategy_success": strategy_stats(student),
        "open_question": (dict(oq) if (oq := get_open_question(student)) else None),
        "chapter_tests_due": get_chapter_tests_due(student),
        "progress_by_subject": get_progress(student),
    }

# ---------- LLM turn ----------
SYSTEM = """You are LearnLoop, a study buddy with long-term memory of what this student has learned.
Your memory comes from a knowledge graph with three layers:
- short_term_recent_messages: the latest conversation turns (use them to follow the thread)
- class_lessons_from_teacher: what the teacher taught this student's class, and which of those
  concepts the student has not revised yet (revised_by_student: false)
- long_term_concepts_weakest_first: each concept's memory_strength (forgetting curve),
  status (fresh / fading / FORGETTING), weak prerequisites and palace_cue
- reasoning_strategy_success: which revision strategies have worked for THIS student before
Behaviour:
- If the student tells you what they learned today: acknowledge it, extract concepts, and ask
  1 short quiz question about it.
- open_question: the quiz question you asked last, with its expected answer, still waiting.
  If the new message answers it, grade ONLY against open_question.expected_answer.
  Answers like "no", "idk", "don't know", blank, or unrelated text are INCORRECT: say so kindly,
  give the correct answer, and record correct=false for its concepts. Never grade against an
  older question. Only mark correct if the answer really matches the expected answer.
- If they answer a quiz question: judge it, correct gently, and record the result.
- If they ask about tomorrow / the next class / a preview: use upcoming_class_preview. Give a quick
  overview a student can read in 1 minute: the topic in one line, 3-5 bullets on what will be
  covered (simple words, from the teacher's notes), and "it builds on" - link to concepts they
  already know. If any builds_on concept "needs revision" or is "not studied yet", tell them to
  revise that tonight. End with one curiosity question (not graded; decision may be null).
- If they ask to revise today's class / catch up on a missed class: use class_lessons_from_teacher,
  start with concepts they have not revised yet, give a 2-line recap, then quiz one concept.
  Use the teacher's concept names exactly in quiz_results.
- When teacher_notes exist for a concept, base your recap, quiz question and answer checking on
  those notes (the teacher's wording and examples). Use this memory naturally; do not keep
  announcing where it came from (mention the teacher's notes at most once per conversation).
- If they ask what to study / revise / plan: prioritise FORGETTING then fading concepts, and if a
  concept has weak prerequisites, revise those first. Refer to what they learned and when.
- Prefer the strategy with the highest success_rate for this student, and mention it briefly
  (e.g. "palace cues have worked well for you").
- Always ask at most one quiz question at a time. Keep replies short and encouraging.
Fortnightly review (every 2 weeks, cumulative over the whole term so far):
- fortnight_review.due = true means a 2-week revision test is due. It covers everything taught so
  far; concepts_to_test_next is already prioritised from this student's own answer history
  (see each "why"). If they say "review" or ask what to do while it is due, run it: one question
  at a time, next concept from concepts_to_test_next, strategy "fortnight_review" and the
  review_id in your decision, questions based on teacher_notes.
- When no concepts are left, give the result: score, and the list of topics that need revision
  and why (from their answers), plus when the next review is (next_review_in_days).
- If they ask "what should I revise?", use the same priorities and explain the reasons briefly.
Chapter tests and long-term progress:
- chapter_tests_due lists finished chapters and the concepts still untested for this student.
  If they say "test", "chapter test", or ask what to do and a test is due, run the chapter test:
  one question at a time, one per concept in concepts_left_to_test, with strategy "chapter_test"
  and the chapter_id in your decision. When the last concept is answered, give the score and
  name the concepts to revise.
- progress_by_subject shows each chapter's status, avg_memory and chapter_test score, plus
  weekly_results over time. If they ask "how am I doing", summarise it per chapter and per week,
  point out the weakest chapter and the trend, and say whether the subject is complete
  (subject_complete true = all chapters covered; then give an overall summary).
Memory palace (method of loci):
- If the student describes familiar places in order (e.g. "my home: gate, sofa, fridge, bed"),
  record them as palace_loci in that order.
- When they learn a new concept and free_loci exist, place it at the next free locus with a
  short vivid image linking the concept to that place, and tell them the image.
- When a concept is fading or FORGETTING and has a palace_cue, you may start the revision by
  walking them to that place: "Picture your <locus>... what did you leave there?"
- If they have no palace yet and have learned 2+ concepts, you may offer to set one up.
Whenever you ask a quiz question, record your decision, including the exact question and the
expected answer (from the teacher's notes when available). Your reason is your own analysis of the
memory (strength, prerequisites, what worked before) - keep it for the record, not the reply.
  strategy is one of: plain_quiz, palace_cue, prerequisite_first, explain_then_quiz, chapter_test,
  fortnight_review
Return ONLY JSON:
{
 "reply": "message to the student",
 "learned": [{"concept": "...", "topic": "...", "prerequisites": ["..."]}],
 "quiz_results": [{"concept": "...", "correct": true}],
 "palace_name": "name of the palace if newly described, else null",
 "palace_loci": ["place 1", "place 2"],
 "placements": [{"concept": "...", "locus": "...", "image": "vivid image"}],
 "decision": {"strategy": "...", "target_concepts": ["..."], "reason": "why, citing memory",
              "question": "the exact question you asked", "expected_answer": "...",
              "chapter_id": "only for chapter_test, else null",
              "review_id": "only for fortnight_review, else null"} or null,
 "session_summary": "one line on what happened this turn, or null"
}"""

def parse_json(text):
    """Parse the model's JSON, tolerating code fences or text around it."""
    t = (text or "").strip()
    if t.startswith("```"):
        t = t.strip("`")
        t = t[t.find("{"):]
    t = t[t.find("{"): t.rfind("}") + 1]
    return json.loads(t)

def ask(student, message):
    ctx = get_context(student)
    user = f"STUDENT: {student}\nMEMORY:\n{json.dumps(ctx, indent=1)}\n\nNEW MESSAGE:\n{message}"
    try:
        return parse_json(llm(SYSTEM, user))
    except json.JSONDecodeError:
        # one automatic retry, asking for a shorter strictly-valid reply
        return parse_json(llm(SYSTEM, user + "\n\nIMPORTANT: your last reply was not valid JSON. "
                                "Return ONLY one valid JSON object, keep the reply and images short."))

# ---------- write everything back ----------
def save(student, user_msg_id, out):
    t = now()
    open_q = get_open_question(student)  # the question this message is answering, if any
    is_test = bool(open_q and open_q["strategy"] == "chapter_test")
    is_review = bool(open_q and open_q["strategy"] == "fortnight_review" and open_q["review_id"])
    kind = "chapter_test" if is_test else "fortnight_review" if is_review else "quiz"
    with driver.session() as s:
        # --- long-term: concepts ---
        for c in out.get("learned") or []:
            s.run("""
            MATCH (st:Student {name:$s})
            MERGE (c:Concept {name:toLower($c)})
            MERGE (st)-[k:KNOWS]->(c)
              ON CREATE SET k.stability=1.0, k.last_reviewed=$t, k.reviews=0, k.correct=0
            WITH c WHERE $topic IS NOT NULL
            MERGE (tp:Topic {name:toLower($topic)})
            MERGE (c)-[:PART_OF]->(tp)
            """, s=student, c=c["concept"], topic=c.get("topic"), t=t)
            for p in c.get("prerequisites") or []:
                s.run("MATCH (c:Concept {name:toLower($c)}) "
                      "MERGE (p:Concept {name:toLower($p)}) "
                      "MERGE (p)-[:PREREQUISITE_OF]->(c)", c=c["concept"], p=p)
        # --- long-term: quiz results update the forgetting curve ---
        for q in out.get("quiz_results") or []:
            ok = bool(q.get("correct"))
            s.run("""
            MATCH (st:Student {name:$s})
            MERGE (c:Concept {name:toLower($c)})
            MERGE (st)-[k:KNOWS]->(c)
              ON CREATE SET k.stability=1.0, k.reviews=0, k.correct=0
            SET k.stability = CASE WHEN $ok THEN k.stability*2.5 ELSE 1.0 END,
                k.last_reviewed=$t, k.reviews=k.reviews+1,
                k.correct = k.correct + CASE WHEN $ok THEN 1 ELSE 0 END
            """, s=student, c=q["concept"], ok=ok, t=t)
            # --- long-term history: every answer is kept as a dated Attempt (weeks/term view) ---
            s.run("""
            MATCH (st:Student {name:$s}), (c:Concept {name:toLower($c)})
            CREATE (st)-[:ATTEMPTED]->(a:Attempt {id:$id, ts:$t, correct:$ok, kind:$kind,
                                                   question:$qn})-[:ON]->(c)
            WITH a
            OPTIONAL MATCH (ch:Chapter {id:$chid})
            FOREACH (_ IN CASE WHEN ch IS NULL THEN [] ELSE [1] END | CREATE (a)-[:FOR_CHAPTER]->(ch))
            WITH a
            MATCH (st:Student {name:$s})
            FOREACH (_ IN CASE WHEN $rid IS NULL THEN [] ELSE [1] END |
              MERGE (r:Review {id:$rid}) ON CREATE SET r.cycle=$cycle, r.started=$t
              MERGE (st)-[:TOOK_REVIEW]->(r)
              CREATE (a)-[:FOR_REVIEW]->(r))
            """, s=student, c=q["concept"], id=new_id(), t=t, ok=ok, kind=kind,
                 qn=open_q["question"] if open_q else None,
                 chid=open_q["chapter_id"] if is_test else None,
                 rid=open_q["review_id"] if is_review else None,
                 cycle=int(open_q["review_id"].split("|")[-1]) if is_review else None)
        # --- reasoning: close the open question with the outcome ---
        if out.get("quiz_results"):
            all_ok = all(bool(q.get("correct")) for q in out["quiz_results"])
            s.run("""
            MATCH (:Student {name:$s})-[:HAS_TRACE]->(tr:ReasoningTrace)
            WHERE tr.success IS NULL AND tr.skipped IS NULL
            WITH tr ORDER BY tr.ts DESC LIMIT 1
            SET tr.success=$ok, tr.closed=$t
            """, s=student, ok=all_ok, t=t)
        # --- long-term: memory palace ---
        if out.get("palace_loci"):
            pal = out.get("palace_name") or "My palace"
            s.run("MATCH (st:Student {name:$s}) "
                  "MERGE (st)-[:HAS_PALACE]->(:Palace {id:$s+'|'+$p, name:$p})", s=student, p=pal)
            start = s.run("MATCH (:Student {name:$s})-[:HAS_PALACE]->(:Palace)-[h:HAS_LOCUS]->() "
                          "RETURN coalesce(max(h.order), 0) AS m", s=student).single()["m"]
            for i, loc in enumerate(out["palace_loci"], start=1):
                s.run("""
                MATCH (pal:Palace {id:$s+'|'+$p})
                MERGE (l:Locus {id:$s+'|'+toLower($l)}) ON CREATE SET l.name=toLower($l)
                MERGE (pal)-[h:HAS_LOCUS]->(l) ON CREATE SET h.order=$o
                """, s=student, p=pal, l=loc, o=start + i)
        for pl in out.get("placements") or []:
            s.run("""
            MATCH (l:Locus {id:$s+'|'+toLower($l)})
            MERGE (c:Concept {name:toLower($c)})
            MERGE (c)-[r:PLACED_AT]->(l) SET r.image=$img
            """, s=student, l=pl["locus"], c=pl["concept"], img=pl.get("image"))
        # --- short-term: link the message to the concepts it mentioned ---
        mentioned = {c["concept"] for c in out.get("learned") or []} | \
                    {q["concept"] for q in out.get("quiz_results") or []}
        for c in mentioned:
            s.run("MATCH (m:Message {id:$m}), (c:Concept {name:toLower($c)}) MERGE (m)-[:MENTIONS]->(c)",
                  m=user_msg_id, c=c)
        # --- reasoning: open a new decision trace ---
        d = out.get("decision")
        if d and d.get("strategy"):
            tid = new_id()
            # any older unanswered question is superseded by this new one
            s.run("MATCH (:Student {name:$s})-[:HAS_TRACE]->(tr:ReasoningTrace) "
                  "WHERE tr.success IS NULL AND tr.skipped IS NULL SET tr.skipped=true", s=student)
            s.run("""
            MATCH (st:Student {name:$s}), (m:Message {id:$m})
            CREATE (m)-[:TRIGGERED]->(tr:ReasoningTrace {id:$id, strategy:$st, reason:$r, ts:$t,
                                                         question:$qn, expected_answer:$ea,
                                                         chapter_id:$chid, review_id:$rid})
            CREATE (st)-[:HAS_TRACE]->(tr)
            """, s=student, m=user_msg_id, id=tid, st=d["strategy"], r=d.get("reason"), t=t,
                 qn=d.get("question"), ea=d.get("expected_answer"), chid=d.get("chapter_id"),
                 rid=d.get("review_id"))
            for c in d.get("target_concepts") or []:
                s.run("MATCH (tr:ReasoningTrace {id:$id}) MERGE (c:Concept {name:toLower($c)}) "
                      "MERGE (tr)-[:TARGETED]->(c)", id=tid, c=c)

# ---------- demo printouts ----------
def print_revise(student):
    """What this student needs to revise, and why — computed from their own answers over the term."""
    pr = revision_priorities(student)
    if not pr:
        print("  Nothing covered in class yet.")
        return
    print("  Revision priorities (from your answers so far, most urgent first):")
    for p in pr[:8]:
        print(f'    {p["concept"]:<24} priority {p["priority"]:<5} - {p["why"]}  (taught {p["taught_days_ago"]} days ago)')
    fr = get_fortnight_review(student)
    if fr:
        if fr["due"]:
            print(f'  2-week review #{fr["cycle"]} is DUE ({fr["covers"]}) - score so far {fr["score_so_far"]}. Type "review".')
        else:
            print(f'  Next 2-week review in {fr["next_review_in_days"]} days.')
def print_progress(student):
    p = get_progress(student)
    if not p["subjects"]:
        print("  No class lessons yet (join a class the teacher has logged lessons for).")
    for sub, info in p["subjects"].items():
        print(f"  {sub.upper()}" + ("  - SUBJECT COMPLETE: all chapters covered" if info["subject_complete"] else ""))
        for c in info["chapters"]:
            print(f'    {c["chapter"]:<22} [{c["status"]:<11}] memory {"#" * int(c["avg_memory"] * 10):<10}'
                  f' {c["avg_memory"]:.2f}   chapter test: {c["chapter_test"]}')
    if p["weekly_results"]:
        print("  Week by week (all quiz + test answers):")
        for w in p["weekly_results"]:
            pct = int(100 * w["correct"] / w["answered"]) if w["answered"] else 0
            print(f'    Week {w["week"]}: {w["correct"]}/{w["answered"]} correct ({pct}%)')
    due = get_chapter_tests_due(student)
    for d in due:
        print(f'  Chapter test due: {d["chapter"]} ({len(d["concepts_left_to_test"])} questions left) - type "test"')
def print_status(student):
    knowledge, _ = get_knowledge(student)
    if not knowledge:
        print("  Nothing learned yet.")
    for m in knowledge:
        bar = "#" * int(m["memory_strength"] * 10)
        print(f'  {m["concept"]:<28} {bar:<10} {m["memory_strength"]:.2f} {m["status"]}'
              + (f'  (weak prereq: {", ".join(m["weak_prerequisites"])})' if m["weak_prerequisites"] else "")
              + (f'  @ {m["palace_cue"]["locus"]}' if m.get("palace_cue") else ""))

def print_palace(student):
    rows = get_palace(student)
    if not rows:
        print("  No memory palace yet. Describe familiar places, e.g. 'My home: gate, sofa, fridge, bed'")
    for r in rows:
        print(f'  {r["order"]}. {r["locus"]:<16} -> {r["concept"] or "(empty)"}'
              + (f'  [{r["image"]}]' if r["image"] else ""))

def print_why(student):
    rows = recent_traces(student)
    if not rows:
        print("  No decisions recorded yet.")
    for r in rows:
        result = ("skipped" if r["skipped"] else "worked" if r["success"]
                  else "waiting for answer" if r["success"] is None else "did not work")
        print(f'  [{r["strategy"]}] on {", ".join(r["concepts"])} -> {result}'
              + (f'\n      asked: {r["question"]}' if r["question"] else "")
              + f'\n      reason: {r["reason"]}')

def print_stats(student):
    rows = strategy_stats(student)
    if not rows:
        print("  No completed decisions yet — answer a few quiz questions first.")
    for r in rows:
        print(f'  {r["strategy"]:<20} {r["worked"]}/{r["tries"]} worked  ({int(r["success_rate"] * 100)}%)')

# =====================================================================
# TEACHER MODE — log today's lesson for a class, see what the class is forgetting
# =====================================================================
TEACHER_SYSTEM = """You help a teacher log a lesson for their class.
The teacher may send a short description or full class notes, for a lesson taught today or for an
upcoming class (e.g. "tomorrow I will teach...", or the message says it is for the NEXT class).
Extract the subject and the key concepts (3-8 short noun phrases, lowercase), with
prerequisites if obvious (prefer concepts the class already studied). For each concept, copy the
key points from the teacher's text into "note" (2-5 lines, keep the teacher's own wording and
examples; empty string if none).
Return ONLY JSON:
{
 "when": "today" or "upcoming",
 "days_ahead": 0 for today, 1 for tomorrow, etc.,
 "subject": "biology",
 "chapter": "the chapter / unit this lesson belongs to, e.g. photosynthesis (lowercase)",
 "chapter_complete": true only if the teacher says this lesson finishes the chapter, else false,
 "concepts": [{"concept": "...", "prerequisites": ["..."], "note": "teacher's key points"}],
 "summary": "one-line recap (or preview, if upcoming) a student could read",
 "reply": "short confirmation to the teacher listing the concepts (and notes) saved, and for when"
}"""

def save_lesson(teacher, class_name, out, force_upcoming=False):
    lid = new_id()
    upcoming = force_upcoming or out.get("when") == "upcoming"
    days_ahead = max(1, int(out.get("days_ahead") or 1)) if upcoming else 0
    # a lesson dated in the future is "planned"; once that time passes it counts as taught
    lesson_ts = now() + days_ahead * DAY
    with driver.session() as s:
        s.run("""
        MERGE (t:Teacher {name:$t})
        MERGE (cl:Class {name:$c})
        MERGE (t)-[:TEACHES]->(cl)
        CREATE (cl)-[:HAD_LESSON]->(l:Lesson {id:$id, subject:toLower($sub), summary:$sum, ts:$ts,
                                               planned:$planned})
        CREATE (t)-[:TAUGHT]->(l)
        """, t=teacher, c=class_name, id=lid, sub=out.get("subject") or "general",
             sum=out.get("summary"), ts=lesson_ts, planned=upcoming)
        # Subject -> Chapter structure, so progress can be tracked chapter by chapter over weeks
        sub = (out.get("subject") or "general").lower()
        chap = (out.get("chapter") or sub).lower()
        s.run("""
        MATCH (cl:Class {name:$c}), (l:Lesson {id:$id})
        MERGE (su:Subject {id:$c + '|' + $sub}) ON CREATE SET su.name=$sub
        MERGE (cl)-[:STUDIES]->(su)
        MERGE (ch:Chapter {id:$c + '|' + $sub + '|' + $chap})
          ON CREATE SET ch.name=$chap, ch.status='in_progress', ch.started=$ts
        MERGE (su)-[:HAS_CHAPTER]->(ch)
        CREATE (l)-[:PART_OF_CHAPTER]->(ch)
        """, c=class_name, id=lid, sub=sub, chap=chap, ts=lesson_ts)
        if out.get("chapter_complete") and not upcoming:
            finish_chapter(class_name, chap)
        for c in out.get("concepts") or []:
            s.run("""
            MATCH (l:Lesson {id:$id})
            MERGE (c:Concept {name:toLower($c)})
            MERGE (l)-[:INCLUDES]->(c)
            MERGE (tp:Topic {name:toLower($sub)})
            MERGE (c)-[:PART_OF]->(tp)
            """, id=lid, c=c["concept"], sub=out.get("subject") or "general")
            if (c.get("note") or "").strip():
                # teacher's notes, chunked per concept: (Lesson)-[:HAS_NOTE]->(Note)-[:EXPLAINS]->(Concept)
                s.run("""
                MATCH (l:Lesson {id:$id}), (c:Concept {name:toLower($c)})
                CREATE (l)-[:HAS_NOTE]->(:Note {id:$nid, text:$txt, ts:$ts})-[:EXPLAINS]->(c)
                """, id=lid, c=c["concept"], nid=new_id(), txt=c["note"].strip(), ts=now())
            for p in c.get("prerequisites") or []:
                s.run("MATCH (c:Concept {name:toLower($c)}) MERGE (p:Concept {name:toLower($p)}) "
                      "MERGE (p)-[:PREREQUISITE_OF]->(c)", c=c["concept"], p=p)

def class_report(class_name):
    q = """
    MATCH (cl:Class {name:$c})-[:HAD_LESSON]->(les:Lesson)-[:INCLUDES]->(con:Concept)
    WHERE les.ts <= $now
    OPTIONAL MATCH (cl)<-[:ENROLLED_IN]-(s:Student)
    OPTIONAL MATCH (s)-[k:KNOWS]->(con)
    RETURN con.name AS concept, s.name AS student, k.stability AS stability, k.last_reviewed AS last
    """
    with driver.session() as s:
        rows = s.run(q, c=class_name, now=now()).data()
    if not rows:
        print("  No lessons logged for this class yet.")
        return
    by_concept = {}
    for r in rows:
        d = by_concept.setdefault(r["concept"], {"strengths": [], "not_revised": [], "forgetting": []})
        if r["student"] is None:
            continue
        if r["stability"] is None:
            d["not_revised"].append(r["student"])
            continue
        R = retention(r["stability"], r["last"])
        d["strengths"].append(R)
        if R < 0.7:
            d["forgetting"].append(r["student"])
    print(f"  Class {class_name} — concept health (weakest first)")
    items = sorted(by_concept.items(),
                   key=lambda kv: sum(kv[1]["strengths"]) / len(kv[1]["strengths"]) if kv[1]["strengths"] else 0)
    for concept, d in items:
        avg = sum(d["strengths"]) / len(d["strengths"]) if d["strengths"] else 0
        print(f'  {concept:<26} {"#" * int(avg * 10):<10} {avg:.2f}'
              + (f'  forgetting: {", ".join(sorted(set(d["forgetting"])))}' if d["forgetting"] else "")
              + (f'  not revised: {", ".join(sorted(set(d["not_revised"])))}' if d["not_revised"] else ""))

def finish_chapter(class_name, chapter):
    """Teacher marks a chapter finished -> every student in the class gets a chapter test."""
    with driver.session() as s:
        rec = s.run("""
        MATCH (:Class {name:$c})-[:STUDIES]->(:Subject)-[:HAS_CHAPTER]->(ch:Chapter)
        WHERE ch.name = toLower($ch) OR ch.name CONTAINS toLower($ch)
        SET ch.status='finished', ch.finished=$t
        RETURN collect(ch.name) AS names
        """, c=class_name, ch=chapter, t=now()).single()
    return rec["names"] if rec else []

def list_chapters(class_name):
    with driver.session() as s:
        return s.run("""
        MATCH (:Class {name:$c})-[:STUDIES]->(su:Subject)-[:HAS_CHAPTER]->(ch:Chapter)
        RETURN su.name AS subject, ch.name AS chapter, ch.status AS status ORDER BY ch.started
        """, c=class_name).data()

def subject_report(class_name):
    """Per chapter: status, class average chapter-test score and memory, students at risk."""
    q = """
    MATCH (cl:Class {name:$c})-[:STUDIES]->(su:Subject)-[:HAS_CHAPTER]->(ch:Chapter)
    OPTIONAL MATCH (cl)<-[:ENROLLED_IN]-(s:Student)
    OPTIONAL MATCH (s)-[:ATTEMPTED]->(a:Attempt {kind:'chapter_test'})-[:FOR_CHAPTER]->(ch)
    WITH su, ch, s, count(a) AS answered, sum(CASE WHEN a.correct THEN 1 ELSE 0 END) AS correct
    RETURN su.name AS subject, ch.name AS chapter, ch.status AS status, ch.started AS started,
           collect({student:s.name, answered:answered, correct:correct}) AS results
    ORDER BY subject, started
    """
    with driver.session() as s:
        rows = s.run(q, c=class_name).data()
    if not rows:
        print("  No chapters yet for this class.")
        return
    current, all_done = None, {}
    for r in rows:
        all_done.setdefault(r["subject"], []).append(r["status"] == "finished")
    for r in rows:
        if r["subject"] != current:
            current = r["subject"]
            done = all(all_done[current])
            print(f"  {current.upper()}" + ("  - SUBJECT COMPLETE: all chapters covered" if done else ""))
        taken = [x for x in r["results"] if x["student"] and x["answered"]]
        missing = sorted(x["student"] for x in r["results"] if x["student"] and not x["answered"])
        if taken:
            avg = sum(x["correct"] / x["answered"] for x in taken) / len(taken)
            at_risk = sorted(x["student"] for x in taken if x["correct"] / x["answered"] < 0.6)
            line = f"class test avg {int(avg * 100)}%"
            if at_risk:
                line += f"  at risk: {', '.join(at_risk)}"
        else:
            line = "no tests taken yet"
        print(f'    {r["chapter"]:<22} [{r["status"]:<11}] {line}'
              + (f"  not taken: {', '.join(missing)}" if missing and r["status"] == "finished" else ""))

def reviews_report(class_name):
    """Every 2-week review, per student: score and the concepts they got wrong."""
    q = """
    MATCH (:Class {name:$c})<-[:ENROLLED_IN]-(s:Student)-[:TOOK_REVIEW]->(r:Review)<-[:FOR_REVIEW]-(a:Attempt)-[:ON]->(con:Concept)
    RETURN s.name AS student, r.cycle AS cycle, count(a) AS answered,
           sum(CASE WHEN a.correct THEN 1 ELSE 0 END) AS correct,
           [x IN collect(CASE WHEN a.correct THEN NULL ELSE con.name END) WHERE x IS NOT NULL] AS wrong
    ORDER BY cycle, student
    """
    with driver.session() as s:
        rows = s.run(q, c=class_name).data()
    if not rows:
        print("  No 2-week reviews taken yet.")
        return
    weak = {}
    for r in rows:
        pct = int(100 * r["correct"] / r["answered"]) if r["answered"] else 0
        print(f'  Review #{r["cycle"]}  {r["student"]:<12} {r["correct"]}/{r["answered"]} ({pct}%)'
              + (f'  revise: {", ".join(r["wrong"])}' if r["wrong"] else ""))
        for w in r["wrong"]:
            weak[w] = weak.get(w, 0) + 1
    if weak:
        top = sorted(weak.items(), key=lambda kv: -kv[1])[:5]
        print("  Class-wide weak spots: " + ", ".join(f"{k} ({v})" for k, v in top))

def read_teacher_notes(msg):
    """'notes' -> paste lines until END; 'notes path.txt' -> read a text file."""
    parts = msg.split(maxsplit=1)
    if len(parts) > 1:
        path = parts[1].strip().strip('"')
        with open(path, encoding="utf-8", errors="ignore") as f:
            return f.read()
    print("  Paste the notes. Type END on its own line when done.")
    lines = []
    while True:
        line = input()
        if line.strip() == "END":
            break
        lines.append(line)
    return "\n".join(lines)

def teacher_loop(teacher):
    global day_offset
    class_name = input("Class (e.g. 8A): ").strip().upper()
    print("  Type what you taught today, 'notes' to paste today's notes (or 'notes file.txt'),\n"
          "  'plan' / 'plan file.txt' for tomorrow's class, 'chapters', 'finish <chapter>',\n"
          "  'report' (concept memory), 'subject' (chapter tests), 'reviews' (2-week reviews),\n"
          "  'skip N', or 'quit'.")
    while True:
        msg = input(f"\n[Teacher {teacher} | {class_name} | day +{day_offset}] > ").strip()
        if not msg:
            continue
        if msg == "quit":
            break
        if msg.startswith("skip"):
            parts = msg.split()
            day_offset += int(parts[1]) if len(parts) > 1 and parts[1].isdigit() else 1
            print(f"  ...{day_offset} day(s) later")
            continue
        if msg == "report":
            class_report(class_name)
            continue
        if msg == "subject":
            subject_report(class_name)
            continue
        if msg == "reviews":
            reviews_report(class_name)
            continue
        if msg == "chapters":
            for c in list_chapters(class_name):
                print(f'  {c["subject"]:<12} {c["chapter"]:<24} [{c["status"]}]')
            continue
        if msg.startswith("finish"):
            name = msg[6:].strip()
            if not name:
                print("  Usage: finish <chapter name>   (type 'chapters' to see them)")
                continue
            done = finish_chapter(class_name, name)
            print(f"  Finished: {', '.join(done)} - chapter test is now due for every student in {class_name}"
                  if done else "  No matching chapter. Type 'chapters' to see them.")
            continue
        force_upcoming = False
        if msg == "plan" or msg.startswith("plan "):
            try:
                msg = ("Notes for the NEXT class (tomorrow, upcoming, not taught yet):\n"
                       + read_teacher_notes("notes" + msg[4:]))
            except OSError as e:
                print(f"  Could not read that file: {e}")
                continue
            force_upcoming = True
        elif msg == "notes" or msg.startswith("notes "):
            try:
                msg = "Today's class notes:\n" + read_teacher_notes(msg)
            except OSError as e:
                print(f"  Could not read that file: {e}")
                continue
        try:
            try:
                out = parse_json(llm(TEACHER_SYSTEM, msg))
            except json.JSONDecodeError:
                out = parse_json(llm(TEACHER_SYSTEM, msg + "\n\nReturn ONLY one valid JSON object; keep notes short."))
        except json.JSONDecodeError:
            print("  (the model returned something unexpected — please send that again)")
            continue
        save_lesson(teacher, class_name, out, force_upcoming)
        print(f"\nLearnLoop: {out['reply']}")

def main():
    global day_offset
    with driver.session() as s:
        for label, prop in [("Concept", "name"), ("Student", "name"), ("Message", "id"),
                            ("ReasoningTrace", "id"), ("Class", "name")]:
            try:
                s.run(f"CREATE CONSTRAINT IF NOT EXISTS FOR (n:{label}) REQUIRE n.{prop} IS UNIQUE")
            except Exception:
                pass  # an index already exists for this (e.g. created by another tool) - fine
    role = input("Are you a (t)eacher or (s)tudent? ").strip().lower()
    if role.startswith("t"):
        teacher_loop(input("Teacher name: ").strip())
        return
    student = input("Student name: ").strip()
    class_name = input("Class (e.g. 8A, or press Enter to skip): ").strip().upper()
    if class_name:
        with driver.session() as s:
            s.run("MERGE (st:Student {name:$s}) MERGE (cl:Class {name:$c}) MERGE (st)-[:ENROLLED_IN]->(cl)",
                  s=student, c=class_name)
    start_conversation(student)
    commands = {"status": print_status, "palace": print_palace, "why": print_why, "stats": print_stats,
                "progress": print_progress, "revise": print_revise}
    while True:
        msg = input(f"\n[{student} | day +{day_offset}] > ").strip()
        if not msg:
            continue
        if msg == "quit":
            break
        if msg.startswith("skip"):
            parts = msg.split()
            day_offset += int(parts[1]) if len(parts) > 1 and parts[1].isdigit() else 1
            print(f"  ...{day_offset} day(s) later")
            start_conversation(student)  # a new day is a new conversation
            continue
        if msg in commands:
            commands[msg](student)
            continue
        if msg == "tomorrow":
            msg = "Give me a quick preview of my next class."
        if msg == "test":
            msg = "Start my chapter test (or continue it)."
        if msg == "review":
            msg = "Start my 2-week revision review (or continue it)."
        try:
            out = ask(student, msg)
        except json.JSONDecodeError:
            print("  (the model returned something unexpected — please send that again)")
            continue
        user_msg_id = add_message("student", msg)
        save(student, user_msg_id, out)
        add_message("agent", out["reply"])
        print(f"\nLearnLoop: {out['reply']}")

if __name__ == "__main__":
    main()
