# LearnLoop 🧠🔁

**An AI study buddy that remembers what you learned — so you don't forget it.**

Built at the *Neo4j Agent Memory Build Sprint, Pune* (hackFront India 2026, Open Track).

Students forget most of a new lesson within days, and teachers usually find out only at the exam.
LearnLoop gives every student a personal revision loop built on the teacher's own class notes, and
uses **Neo4j as the agent's memory** to know what each student knows, how strongly they remember it,
*why* they are struggling, and which revision strategy works for them.

---

## What it does

| For students | For teachers |
|---|---|
| Revise today's class from the teacher's notes | Log a lesson or paste class notes once |
| Quick preview of tomorrow's class, linked to what you already know | Plan the next class |
| Quizzes graded against the teacher's notes | `report` — which concepts the class is forgetting, and who hasn't revised |
| Forgetting-curve memory strength per concept | `finish <chapter>` — unlock a chapter test for every student |
| Memory palace (method of loci) cues | `subject` — chapter test results, students at risk |
| Chapter tests and a 2-week cumulative review, prioritised from your own answers | `reviews` — every 2-week review, class-wide weak spots |

## Memory techniques

- **Method of loci (memory palace)** — concepts are placed at familiar locations; recall starts by "walking" there.
- **Spaced repetition** — each concept's memory strength follows the forgetting curve `R = e^(-t/S)`; a correct answer makes it last ~2.5× longer, a wrong one resets it.
- **Active recall** — the agent always quizzes instead of re-showing notes.

## Why Neo4j — the context graph

All three agent-memory layers live in **one connected graph**:

| Layer | Graph |
|---|---|
| **Short-term** (what happened) | `(Student)-[:HAS_CONVERSATION]->(Conversation)-[:FIRST_MESSAGE]->(Message)-[:NEXT_MESSAGE]->(Message)`, `(Message)-[:MENTIONS]->(Concept)` |
| **Long-term** (what the student knows) | `(Student)-[:KNOWS {stability, last_reviewed, reviews, correct}]->(Concept)`, `(Concept)-[:PREREQUISITE_OF]->(Concept)`, `(Student)-[:ATTEMPTED]->(Attempt)-[:ON]->(Concept)`, memory palace `(Palace)-[:HAS_LOCUS]->(Locus)<-[:PLACED_AT]-(Concept)` |
| **Reasoning** (how the agent decided) | `(Message)-[:TRIGGERED]->(ReasoningTrace {strategy, reason, question, success})-[:TARGETED]->(Concept)` |
| **Classroom** | `(Teacher)-[:TEACHES]->(Class)-[:HAD_LESSON]->(Lesson)-[:INCLUDES]->(Concept)`, `(Lesson)-[:HAS_NOTE]->(Note)-[:EXPLAINS]->(Concept)`, `(Class)-[:STUDIES]->(Subject)-[:HAS_CHAPTER]->(Chapter)` |

What the graph makes possible:

- **Root-cause revision** — follow `PREREQUISITE_OF` back to a fading building block ("weak on respiration because photosynthesis is fading").
- **Grounded answers (GraphRAG)** — `Class → Lesson → Concept → Note` finds the teacher's own explanation, from any week of the term.
- **One lesson, every student** — a lesson logged once reaches each enrolled student's personal memory.
- **Agent that learns how to teach** — reasoning traces record which strategy worked for which student.
- **Term-long history** — every answer is a dated `Attempt`, so 2-week reviews are prioritised from each student's own mistakes.

## Quick start

Requirements: Python 3.9+, a free [Neo4j AuraDB](https://console.neo4j.io) instance, and an Anthropic or OpenAI API key.

```bash
git clone https://github.com/<your-username>/learnloop.git
cd learnloop
python -m venv venv
# Windows: venv\Scripts\activate      macOS/Linux: source venv/bin/activate
pip install -r requirements.txt
cp .env.example .env        # Windows: copy .env.example .env
# edit .env with your Neo4j URI / user / password and your API key
python learnloop_agent.py        # terminal version
streamlit run app.py             # web version
```

### Deploy the web app (Streamlit Community Cloud)
1. Push this repo to GitHub (public).
2. Go to share.streamlit.io → **Create app** → pick this repo, branch `main`, file `app.py`.
3. Under **Advanced settings → Secrets**, add:
   ```toml
   NEO4J_URI = "neo4j+s://<instance-id>.databases.neo4j.io"
   NEO4J_USER = "<user>"
   NEO4J_PASSWORD = "<password>"
   ANTHROPIC_API_KEY = "<key>"
   ```
4. Deploy — you get a public URL for the live demo.

> On newer Aura instances the username may be the **instance ID** rather than `neo4j` — check the credentials file you downloaded.

## Demo script (~4 minutes)

**1. Teacher logs today's class**
```
t → Mrs Sharma → 8A
notes samples/sample_notes.txt
plan samples/sample_tomorrow.txt
quit
```

**2. Student revises**
```
s → Aryan → 8A
Revise today's class          (answer one question wrong on purpose)
why                           (the agent's reasoning, from memory)
tomorrow                      (personalised preview of the next class)
skip 14                       (two weeks pass)
revise                        (what to revise and why, from past answers)
review                        (2-week cumulative review)
quit
```

**3. Teacher sees who is forgetting what**
```
t → Mrs Sharma → 8A
skip 14
report
reviews
```

**4. Show the graph** — run the queries in [`docs/queries.cypher`](docs/queries.cypher) in Aura *Query* or *Explore*.

## Commands

**Student:** `status` · `palace` · `why` · `stats` · `progress` · `revise` · `review` · `test` · `tomorrow` · `skip N` · `quit`

**Teacher:** `notes [file]` · `plan [file]` · `chapters` · `finish <chapter>` · `report` · `subject` · `reviews` · `skip N` · `quit`

`skip N` simulates N days passing so the forgetting curve can be demonstrated live.

## Tech stack

- **Neo4j AuraDB** — context graph (short-term, long-term, reasoning memory)
- **Python** + official `neo4j` driver (Bolt)
- **LLM** — Anthropic Claude or OpenAI (auto-selected from the key present)

## Roadmap

- Web / mobile UI for students and a teacher dashboard
- Voice "explain it back" sessions
- Rebuild the memory layer on the `neo4j-agent-memory` SDK (hybrid with custom Cypher)
- Past-paper questions as an extra quiz source
- Parent-consented focus mode during study sessions

## Team

**Hacklon** — Harshal Kadam
