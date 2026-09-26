"""
LearnLoop web app (Streamlit) — a browser front end for learnloop_agent.py.
Run locally:   streamlit run app.py
Deploy:        Streamlit Community Cloud, with NEO4J_URI / NEO4J_USER / NEO4J_PASSWORD /
               ANTHROPIC_API_KEY (or OPENAI_API_KEY) set as secrets.
"""
import io
import json
import contextlib
import streamlit as st

st.set_page_config(page_title="LearnLoop", page_icon="🧠", layout="wide")

try:
    import learnloop_agent as ll
except KeyError as e:
    st.error(f"Missing setting {e}. Add NEO4J_URI, NEO4J_USER, NEO4J_PASSWORD and an API key as secrets.")
    st.stop()


def captured(fn, *args):
    """Run one of the agent's print_* / report functions and return what it printed."""
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        fn(*args)
    return buf.getvalue() or "(nothing yet)"


ss = st.session_state

# ---------------- sidebar ----------------
st.sidebar.title("🧠 LearnLoop")
st.sidebar.caption("An AI study buddy that remembers what you learned, so you don't forget it.")
role = st.sidebar.radio("I am a", ["Student", "Teacher"], horizontal=True)
default_name = "aryan" if role == "Student" else "Mrs Sharma"
name = st.sidebar.text_input("Name", default_name).strip()
class_name = st.sidebar.text_input("Class", "8A").strip().upper()
days = st.sidebar.slider("Demo: days passed", 0, 60, 0,
                         help="Simulate time so the forgetting curve and 2-week reviews can be shown live.")
ll.day_offset = days
st.sidebar.divider()
st.sidebar.caption("Memory lives in a Neo4j context graph: short-term (chat), long-term "
                   "(concepts, memory strength, answers, memory palace) and reasoning (why the "
                   "agent chose each question, and whether it worked).")

if not name or not class_name:
    st.info("Enter a name and class in the sidebar.")
    st.stop()

# ================= STUDENT =================
if role == "Student":
    key = f"{name}|{class_name}"
    if ss.get("student_key") != key:
        with ll.driver.session() as s:
            s.run("MERGE (st:Student {name:$s}) MERGE (cl:Class {name:$c}) MERGE (st)-[:ENROLLED_IN]->(cl)",
                  s=name, c=class_name)
        ll.start_conversation(name)
        ss.student_key, ss.conv_id, ss.last_msg_id, ss.chat = key, ll.conv_id, None, []

    # restore this browser session's conversation pointers into the agent module
    ll.conv_id, ll.last_msg_id = ss.conv_id, ss.last_msg_id

    chat_col, mem_col = st.columns([3, 2])

    with chat_col:
        st.subheader(f"Hi {name} 👋")
        b1, b2, b3, b4 = st.columns(4)
        quick = None
        if b1.button("📘 Revise today's class", use_container_width=True):
            quick = "Revise today's class"
        if b2.button("🔭 Tomorrow's preview", use_container_width=True):
            quick = "Give me a quick preview of my next class."
        if b3.button("🎯 What should I revise?", use_container_width=True):
            quick = "What should I revise today?"
        if b4.button("📝 2-week review", use_container_width=True):
            quick = "Start my 2-week revision review (or continue it)."

        for who, text in ss.chat:
            with st.chat_message("user" if who == "student" else "assistant", avatar=None if who == "student" else "🧠"):
                st.markdown(text)

        msg = st.chat_input("Answer, ask, or describe your memory palace (e.g. 'My home palace: gate, sofa, fridge, bed')")
        msg = msg or quick
        if msg:
            ss.chat.append(("student", msg))
            with st.chat_message("user"):
                st.markdown(msg)
            with st.chat_message("assistant", avatar="🧠"):
                with st.spinner("Checking your memory graph..."):
                    try:
                        out = ll.ask(name, msg)
                        user_msg_id = ll.add_message("student", msg)
                        ll.save(name, user_msg_id, out)
                        ll.add_message("agent", out["reply"])
                        reply = out["reply"]
                    except json.JSONDecodeError:
                        reply = "Sorry, I got confused there. Please send that again."
                st.markdown(reply)
            ss.chat.append(("agent", reply))
            ss.last_msg_id = ll.last_msg_id

    with mem_col:
        st.subheader("🧠 What LearnLoop remembers")
        t1, t2, t3, t4 = st.tabs(["Memory strength", "Revise next", "Memory palace", "Agent reasoning"])
        with t1:
            st.code(captured(ll.print_status, name), language=None)
        with t2:
            st.code(captured(ll.print_revise, name), language=None)
        with t3:
            st.code(captured(ll.print_palace, name), language=None)
        with t4:
            st.code(captured(ll.print_why, name), language=None)
        with st.expander("Progress by chapter and week"):
            st.code(captured(ll.print_progress, name), language=None)

# ================= TEACHER =================
else:
    st.subheader(f"Teacher: {name} · Class {class_name}")
    left, right = st.columns([3, 2])

    with left:
        when = st.radio("This lesson is for", ["Today (taught)", "Tomorrow (plan / preview)"], horizontal=True)
        notes = st.text_area("Paste your class notes or describe what you taught", height=220,
                             placeholder="e.g. Photosynthesis is how green plants make food...")
        uploaded = st.file_uploader("...or upload notes (.txt)", type=["txt"])
        if st.button("💾 Save lesson for the class", type="primary"):
            text = uploaded.read().decode("utf-8", errors="ignore") if uploaded else notes
            if not text.strip():
                st.warning("Add some notes first.")
            else:
                upcoming = when.startswith("Tomorrow")
                prefix = ("Notes for the NEXT class (tomorrow, upcoming, not taught yet):\n" if upcoming
                          else "Today's class notes:\n")
                with st.spinner("Splitting notes into concepts and saving to the graph..."):
                    try:
                        out = ll.parse_json(ll.llm(ll.TEACHER_SYSTEM, prefix + text))
                        ll.save_lesson(name, class_name, out, upcoming)
                        st.success(out.get("reply", "Saved."))
                    except json.JSONDecodeError:
                        st.error("The model returned something unexpected. Please try again.")

        chapters = ll.list_chapters(class_name)
        open_ch = [c["chapter"] for c in chapters if c["status"] != "finished"]
        if open_ch:
            c1, c2 = st.columns([3, 1])
            pick = c1.selectbox("Finish a chapter (unlocks a chapter test for every student)", open_ch)
            if c2.button("✅ Finish", use_container_width=True):
                done = ll.finish_chapter(class_name, pick)
                st.success(f"Finished: {', '.join(done)}")

    with right:
        t1, t2, t3 = st.tabs(["Who is forgetting what", "2-week reviews", "Chapters & tests"])
        with t1:
            st.code(captured(ll.class_report, class_name), language=None)
        with t2:
            st.code(captured(ll.reviews_report, class_name), language=None)
        with t3:
            st.code(captured(ll.subject_report, class_name), language=None)
