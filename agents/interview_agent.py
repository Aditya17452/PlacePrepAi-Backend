import json, asyncio, re
from groq import AsyncGroq
from services.rag_service import retrieve_jd_context
from services.session_service import SessionManager
from core.config import settings

client = AsyncGroq(api_key=settings.GROQ_API_KEY)
client_backup = AsyncGroq(api_key=settings.GROQ_API_KEY2) if getattr(settings, "GROQ_API_KEY2", "") else None

async def _call_groq_with_fallback(messages, max_tokens=120, temperature=0.85):
    try:
        return await client.chat.completions.create(
            model=settings.GROQ_MODEL, messages=messages, max_tokens=max_tokens, temperature=temperature, timeout=20
        )
    except Exception as e:
        print(f"Primary Groq API failed: {e}")
        if client_backup:
            print("Attempting with backup GROQ_API_KEY2...")
            return await client_backup.chat.completions.create(
                model=settings.GROQ_MODEL, messages=messages, max_tokens=max_tokens, temperature=temperature, timeout=20
            )
        raise e

WARMUP_QUESTIONS = [
    "Tell me about yourself — your background, skills, and what drew you to this field.",
    "Walk me through your most significant project — what did you build, what was your role, and what was the biggest challenge?"
]

CLOSING_QUESTIONS = [
    "What is one technical decision you made in a project that you would do differently now, and why?",
    "Where do you see yourself growing technically in the next two years, and what steps are you taking toward that?"
]

FILLERS = ["um", "uh", "like", "you know", "basically", "literally", "so"]

MAX_QUESTIONS = 12


# ── Fetch scraped profile for this student ───────────────────────────────────

def get_student_profile(db, student_id: str) -> str:
    """Fetch scraped GitHub/portfolio/LeetCode summary for this student."""
    try:
        from sqlalchemy import text
        row = db.execute(
            text("SELECT scraped_summary FROM student_profile_data WHERE student_id = :sid"),
            {"sid": student_id}
        ).fetchone()
        return row[0] if row and row[0] else ""
    except Exception as e:
        print(f"Could not fetch student profile: {e}")
        return ""


class InterviewAgent:

    def __init__(self, session_id: str, student: dict, jd: dict):
        self.session_id = session_id
        self.student = student
        self.jd = jd
        self.session = SessionManager(session_id)
        self.session.init(
            student_name=student["name"],
            jd_id=str(jd["id"]),
            jd_role=jd["role_title"]
        )
        # Load scraped profile once at init
        self._profile_context = ""
        self._profile_loaded = False

    def _load_profile(self, db) -> str:
        """Load profile once and cache it."""
        if not self._profile_loaded:
            student_id = str(self.student.get("id", ""))
            if student_id:
                self._profile_context = get_student_profile(db, student_id)
                if self._profile_context:
                    print(f"Loaded scraped profile for {student_id[:8]}... ({len(self._profile_context)} chars)")
                else:
                    print(f"No scraped profile found for {student_id[:8]}...")
            self._profile_loaded = True
        return self._profile_context

    @staticmethod
    def _normalize_question(text: str) -> str:
        lowered = (text or "").lower().strip()
        lowered = re.sub(r"[^a-z0-9\s]", "", lowered)
        return re.sub(r"\s+", " ", lowered).strip()

    @staticmethod
    def _is_uninformative_answer(answer: str) -> bool:
        text = (answer or "").strip().lower()
        if not text:
            return True
        short_no_info = {
            "no", "yes", "ok", "okay", "idk", "i dont know", "i don't know",
            "dont know", "not sure", "n/a", "na"
        }
        if text in short_no_info:
            return True
        return len(text.split()) <= 4

    def _is_repeat_question(self, question: str, memory_questions: list[str]) -> bool:
        normalized = self._normalize_question(question)
        if not normalized:
            return True
        for q in memory_questions:
            prev = self._normalize_question(q)
            if not prev:
                continue
            if normalized == prev:
                return True
            if normalized in prev or prev in normalized:
                return True
        return False

    def _fallback_progression_question(self, q_num: int, answer: str, memory_questions: list[str]) -> str:
        role = self.jd['role_title']
        if self._is_uninformative_answer(answer):
            candidates = [
                f"Let us shift focus. What is one core technical concept you believe is essential for a {role} role, and how have you applied it?",
                f"Could you walk me through a challenging problem you solved recently that is relevant to a {role} position?",
                "Imagine you are given a project with vague requirements. How would you approach clarifying and executing it?",
                f"What part of the {role} role do you find most interesting, and what relevant skills do you bring to it?",
                f"If you had to learn a completely new technology for this {role} role in one week, what would be your strategy?"
            ]
        else:
            candidates = [
                "Can you share one concrete technical decision from your project and the tradeoff you considered?",
                "What metric did you optimize in your project, and why was it better than alternatives?",
                "How did you structure your solution, and what edge cases did you have to handle?",
                "What part of your system was hardest to build, and how did you solve it?",
                "If you had one extra week for that project, what improvement would you prioritize?"
            ]

        for question in candidates:
            if not self._is_repeat_question(question, memory_questions):
                return question

        return f"Let us move forward: what is one technical concept you are most confident in for {self.jd['role_title']} and why?"

    async def get_opening_question(self) -> str:
        # Mark opening question as already asked so first follow-up starts from warmup Q2.
        state = self.session.get_state()
        if state.get("questions_asked", 0) == 0:
            self.session.update({"questions_asked": 1, "phase": "warmup"})
        return WARMUP_QUESTIONS[0]

    async def get_next_question(self, student_answer: str, db=None, asked_question: str | None = None) -> dict:
        state = self.session.get_state()
        q_count = state.get("questions_asked", 0)
        elapsed = self.session.elapsed_minutes()

        memory = self.session.get_memory()
        question_for_memory = asked_question or (memory[-1]["q"] if memory else WARMUP_QUESTIONS[0])
        self.session.add_exchange(question_for_memory, student_answer)

        # END CHECK
        if q_count >= MAX_QUESTIONS or elapsed >= getattr(settings, "HARD_CUTOFF_MIN", 25):
            print(f"Session ending: q_count={q_count}, elapsed={elapsed:.1f}min")
            return {
                "question": None,
                "phase": "complete",
                "questions_asked": q_count,
                "session_complete": True,
                "feedback": None
            }

        new_count = self.session.increment_question()

        # Load scraped profile if db is available
        profile_context = ""
        if db:
            profile_context = self._load_profile(db)

        memory_questions = [m.get("q", "") for m in self.session.get_memory()]

        if new_count <= len(WARMUP_QUESTIONS):
            question = WARMUP_QUESTIONS[new_count - 1]
            phase = "warmup"
        elif new_count == MAX_QUESTIONS - 1:
            question = CLOSING_QUESTIONS[0]
            phase = "closing"
        elif new_count >= MAX_QUESTIONS:
            question = CLOSING_QUESTIONS[1]
            phase = "closing"
        else:
            if self._is_uninformative_answer(student_answer):
                question = self._fallback_progression_question(new_count, student_answer, memory_questions)
            else:
                question = await self._technical_question(student_answer, elapsed, new_count, profile_context)
            phase = "technical"

        if self._is_repeat_question(question, memory_questions):
            question = self._fallback_progression_question(new_count, student_answer, memory_questions)

        feedback = None
        if new_count % getattr(settings, "FEEDBACK_EVERY_N", 3) == 0:
            feedback = await self._live_feedback(student_answer)

        print(f"Q{new_count}/{MAX_QUESTIONS} | phase={phase} | elapsed={elapsed:.1f}min")

        return {
            "question": question,
            "phase": phase,
            "questions_asked": new_count,
            "session_complete": False,
            "feedback": feedback
        }

    async def _technical_question(self, answer: str, elapsed: float, q_num: int, profile_context: str = "") -> str:
        if not answer or answer.strip() in ["", "(no answer recorded)"]:
            answer = "The candidate gave a verbal answer that was not captured clearly."

        jd_context = await retrieve_jd_context(answer, str(self.jd["id"]))
        history = self.session.memory_as_text()
        skills = self.student.get("skills", [])
        skills_str = ", ".join(skills[:5]) if skills else "programming"
        remaining = MAX_QUESTIONS - q_num

        # Build profile section for prompt
        profile_section = ""
        if profile_context:
            profile_section = f"""
CANDIDATE'S ONLINE PROFILE (scraped from their resume links):
{profile_context}

Use this to ask SPECIFIC questions about their actual projects. For example:
- Reference specific repo names, technologies, or README content
- Ask about their LeetCode problem-solving if stats are available
- Ask about sections visible on their portfolio
"""

        asked_qs = "\n".join(f"- {m.get('q', '')}" for m in self.session.get_memory())

        prompt = f"""You are a senior technical interviewer conducting a real placement interview.

ROLE: {self.jd['role_title']}

CANDIDATE:
- Name: {self.student['name']}
- Branch: {self.student.get('branch', 'CS')}, CGPA: {self.student.get('cgpa', 'N/A')}
- Skills: {skills_str}
{profile_section}
ROLE REQUIREMENTS:
{jd_context}

PREVIOUSLY ASKED QUESTIONS (DO NOT ASK THESE OR ANYTHING SIMILAR):
{asked_qs}

CONVERSATION SO FAR:
{history}

STATE: Question {q_num}/{MAX_QUESTIONS} | {remaining} remaining | {elapsed:.0f} min elapsed

ROLE-SPECIFIC TOPICS:
- Frontend/JS: closures, promises, React hooks, virtual DOM, CSS, state management
- ML Engineer: bias-variance, cross-validation, gradient descent, regularization, CNN, deployment
- AI/GenAI: RAG pipeline, embeddings, prompt chaining, LLM temperature, fine-tuning
- Data Engineer: window functions, ETL vs ELT, Spark, DAG design, data modeling
- Data Scientist: p-value, overfitting, A/B testing, confusion matrix, feature importance
- Data Analyst: SQL GROUP BY vs HAVING, pivot tables, KPI definition, cohort analysis
- AIML: neural networks, deep learning, model optimization, feature engineering

RULES:
- NEVER repeat any question already in conversation history. If a similar question was asked, you MUST choose a completely different topic.
- CRITICAL: Focus strictly on evaluating the candidate for the {self.jd['role_title']} role based ONLY on the ROLE REQUIREMENTS provided above.
- DO NOT ask domain-specific questions about their past degrees/branch if they are irrelevant to the current job description.
- IRRELEVANT ANSWER HANDLING: If the candidate's previous answer is completely irrelevant, nonsensical, or dodges the question (e.g. typing gibberish or answering about something completely unrelated), you MUST start your response with: "Your answer seems irrelevant to the question." or "Let's stay on topic." and then ask the next technical question from the JD.
- ONE question only, max 2 sentences.
- NO compliments, NO "Great answer!"
- Sound like a real senior engineer diving deep into their background.
- IMPORTANT: The candidate's answers are generated via Speech-to-Text. Forgive minor typos, homophones, or incorrectly transcribed words. Infer their true meaning based on context.

Output ONLY your response/question. Nothing else."""

        for attempt in range(3):
            try:
                response = await _call_groq_with_fallback(
                    messages=[
                        {"role": "system", "content": prompt},
                        {"role": "user", "content": f"Candidate said: \"{answer[:300]}\"\n\nNext question:"}
                    ],
                    max_tokens=120,
                    temperature=0.85
                )
                question = (response.choices[0].message.content or "").strip().strip('"')
                print(f"Groq Q{q_num}: {question[:80]}")
                return question
            except Exception as e:
                print(f"Groq attempt {attempt + 1} failed: {e}")
                if attempt < 2:
                    await asyncio.sleep(2)

        skills = self.student.get("skills", [])
        fallbacks = [
            f"Can you explain how you used {skills[0] if skills else 'Python'} in a real project?",
            f"What is the most complex technical problem you solved related to {self.jd['role_title']}?",
            "Walk me through how you approach debugging a difficult issue in your code.",
            "How do you decide between two different technical approaches to a problem?",
            "Describe a time you had to learn a new technology quickly — what was your process?",
            f"What core concepts of {self.jd['role_title']} do you feel most confident about?",
            "If you had to optimize your best project for scale, what would you change first?",
        ]
        return fallbacks[q_num % len(fallbacks)]

    async def _live_feedback(self, answer: str) -> dict:
        vision = self.session.vision_summary()
        filler_count = sum(answer.lower().count(f) for f in FILLERS)

        prompt = """You are a supportive interview coach giving quick live feedback.
Be encouraging but specific. Maximum 2 sentences.
Return ONLY valid JSON (no markdown, no backticks):
{"tip": "your coaching tip here", "tone": "positive"}
tone must be exactly one of: positive, neutral, needs_work"""

        user_msg = f"""Answer: "{answer[:300]}"
Stats: {filler_count} filler words. Eye contact: {vision['avg_eye_contact']}%, Posture: {vision['avg_posture']}%.
Give one specific actionable tip."""

        for attempt in range(2):
            try:
                resp = await _call_groq_with_fallback(
                    messages=[
                        {"role": "system", "content": prompt},
                        {"role": "user", "content": user_msg}
                    ],
                    max_tokens=100,
                    temperature=0.5
                )
                raw = (resp.choices[0].message.content or "").strip()
                raw = raw.replace("```json", "").replace("```", "").strip()
                return json.loads(raw)
            except Exception as e:
                print(f"Feedback attempt {attempt + 1} failed: {e}")
                if attempt < 1:
                    await asyncio.sleep(1)

        return {"tip": "Keep giving specific examples with results!", "tone": "positive"}

    async def evaluate_session(self, session_id: str) -> dict:
        from models.database import SessionLocal, SessionTranscript, JobDescription

        db = SessionLocal()
        try:
            rows = db.query(SessionTranscript).filter(
                SessionTranscript.session_id == session_id
            ).order_by(SessionTranscript.turn_number).all()

            if len(rows) < 3:
                print(f"Only {len(rows)} answers recorded — returning incomplete scores")
                return self._incomplete_scores(len(rows))

            transcript_text = "\n".join(
                f"Q{r.turn_number}: {r.question}\nA{r.turn_number}: {r.answer}"
                for r in rows
            )

            jd = db.query(JobDescription).filter(
                JobDescription.id == self.jd["id"]
            ).first()
            jd_text = jd.jd_text if jd else "No JD available"
            vision = self.session.vision_summary()

        finally:
            db.close()

        prompt = """You are a senior technical interviewer and hiring manager evaluating a mock interview.
Evaluate with high professional standards based on the overall impact of the student:
1. Clarity of thought & structure
2. Depth of technical understanding vs shallow buzzwords
3. Problem-solving approach
4. Communication effectiveness

IMPORTANT: The candidate's answers were recorded via Speech-to-Text (whisper). Please FORGIVE and smartly correct minor transcription errors, homophones, or misheard technical jargon. Evaluate their INTENDED meaning based on context.

Evaluate honestly — shallow or short answers should score 20-40, not 65.
A candidate who gave very few answers or one-word replies scores below 30.
Return ONLY valid JSON. No markdown. No backticks.

Required format:
{
  "technical_score": 0-100,
  "communication_score": 0-100,
  "body_language_score": 0-100,
  "overall_score": 0-100,
  "verdict": "Good",
  "topics": [
    {"name": "Topic Name", "score": 0-100, "feedback": "one specific professional sentence"}
  ],
  "strengths": ["strength 1", "strength 2", "strength 3"],
  "improvements": ["area 1", "area 2", "area 3"],
  "tips": ["tip 1", "tip 2", "tip 3"]
}
verdict must be exactly one of: Excellent, Good, Needs Work, Poor
Provide 3-5 topics, 3 strengths, 3 improvements, 3 tips."""

        user_msg = f"""Role: {self.jd['role_title']}

JD Requirements:
{jd_text[:600]}

Interview Transcript ({len(rows)} of 12 questions answered):
{transcript_text[:3000]}

Physical: Eye contact {vision['avg_eye_contact']}%, Posture {vision['avg_posture']}%.

IMPORTANT: Only {len(rows)} questions were answered out of 12.
Score strictly and honestly based on the actual content of answers given.
Do NOT give default 65 scores — evaluate what was actually said."""

        for attempt in range(3):
            try:
                resp = await _call_groq_with_fallback(
                    messages=[
                        {"role": "system", "content": prompt},
                        {"role": "user", "content": user_msg}
                    ],
                    max_tokens=900,
                    temperature=0.3
                )
                raw = (resp.choices[0].message.content or "").strip()
                raw = raw.replace("```json", "").replace("```", "").strip()
                result = json.loads(raw)
                print(f"Evaluation: overall={result.get('overall_score')}, verdict={result.get('verdict')}")
                return result
            except json.JSONDecodeError as e:
                print(f"Eval JSON parse failed attempt {attempt + 1}: {e}")
                if attempt < 2:
                    await asyncio.sleep(3)
            except Exception as e:
                print(f"Eval attempt {attempt + 1} failed: {e}")
                if attempt < 2:
                    await asyncio.sleep(3)

        print("All evaluation attempts failed")
        return self._incomplete_scores(len(rows) if 'rows' in dir() else 0)

    def _incomplete_scores(self, answers_given: int = 0) -> dict:
        if answers_given == 0:
            return {
                "technical_score": 0, "communication_score": 0,
                "body_language_score": 0, "overall_score": 0, "verdict": "Poor",
                "topics": [{"name": "Interview Incomplete", "score": 0,
                             "feedback": "No answers were recorded in this session"}],
                "strengths": ["You started the interview process"],
                "improvements": ["Complete the full 12-question interview",
                                  "Do not end the interview within seconds of starting",
                                  "Prepare at least 20 minutes of uninterrupted time"],
                "tips": ["Start a new interview and answer all 12 questions",
                          "Prepare your answers before clicking Start Interview",
                          "Allow yourself enough time — each session takes 15-20 minutes"]
            }

        completion = answers_given / 12
        base = int(35 * completion)
        verdict = "Poor" if base < 20 else "Needs Work"

        return {
            "technical_score": base, "communication_score": base,
            "body_language_score": base + 10, "overall_score": base, "verdict": verdict,
            "topics": [{"name": "Interview Incomplete", "score": base,
                         "feedback": f"Only {answers_given} of 12 questions were answered"}],
            "strengths": [f"Answered {answers_given} out of 12 questions", "Started the interview session"],
            "improvements": ["Complete all 12 questions for a meaningful evaluation",
                              "Do not end the interview early", "Give detailed answers — not one-word replies"],
            "tips": [f"You completed {answers_given}/12 questions — aim for the full session next time",
                      "Allow 20 minutes of uninterrupted time before starting",
                      "Practice answering questions out loud before your real interview"]
        }

    def _default_scores(self) -> dict:
        return self._incomplete_scores(0)

    async def match_resume_to_jds(self, resume_text: str, jds: list) -> list:
        jd_list = "\n\n".join(
            f"JD {i+1} (id: {jd['id']}):\nRole: {jd['role_title']}\nRequirements: {jd['jd_text'][:400]}"
            for i, jd in enumerate(jds)
        )
        prompt = """You are a strict placement advisor matching a student resume to job roles.
Only give high scores (80-95%) if skills genuinely match.
Return ONLY valid JSON. No markdown. No backticks.
Format:
{
  "ranked": [
    {"jd_id": "uuid", "rank": 1, "match_pct": 0-100,
     "matching_skills": ["skill1"], "missing_skills": ["skill1"], "advice": "one sentence"}
  ]
}"""
        user_msg = f"Student Resume:\n{resume_text[:1500]}\n\nAvailable Roles:\n{jd_list}\n\nRank ALL {len(jds)} roles. Include all JD IDs."

        for attempt in range(3):
            try:
                resp = await client.chat.completions.create(
                    model=settings.GROQ_MODEL,
                    messages=[{"role": "system", "content": prompt},
                               {"role": "user", "content": user_msg}],
                    max_tokens=800, temperature=0.2, timeout=25
                )
                raw = (resp.choices[0].message.content or "").strip().replace("```json","").replace("```","").strip()
                return json.loads(raw).get("ranked", [])
            except Exception as e:
                print(f"Resume match attempt {attempt + 1} failed: {e}")
                if attempt < 2:
                    await asyncio.sleep(2)

        return [{"jd_id": str(jd["id"]), "rank": i+1, "match_pct": 40,
                 "matching_skills": [], "missing_skills": [], "advice": "Default match"}
                for i, jd in enumerate(jds)]

    async def generate_shortlist(self, jd: dict, students: list) -> list:
        student_list = "\n".join(
            f"- ID:{s['id']} Name:{s['name']} CGPA:{s['cgpa']} "
            f"Technical:{s.get('technical_score',0)} Communication:{s.get('communication_score',0)}"
            for s in students
        )
        prompt = """Rank candidates for shortlisting.
Return ONLY valid JSON:
{"ranked": [{"student_id": "uuid", "rank": 1, "match_score": 0-100, "reasoning": "one sentence"}]}"""

        for attempt in range(2):
            try:
                resp = await client.chat.completions.create(
                    model=settings.GROQ_MODEL,
                    messages=[
                        {"role": "system", "content": prompt},
                        {"role": "user", "content": f"Role: {jd['role_title']}\n\nCandidates:\n{student_list}"}
                    ],
                    max_tokens=600, temperature=0.3, timeout=20
                )
                raw = (resp.choices[0].message.content or "").strip().replace("```json","").replace("```","").strip()
                return json.loads(raw).get("ranked", [])
            except Exception as e:
                print(f"Shortlist attempt {attempt + 1} failed: {e}")
                if attempt < 1:
                    await asyncio.sleep(2)
        return []

    def check_posture_warning(self, posture_score: int) -> str | None:
        if posture_score < settings.POSTURE_WARNING_THRESHOLD:
            if self.session.can_warn_posture():
                self.session.mark_warned()
                return "Try to sit up straight and maintain eye contact "
        return None

    def _should_end(self, q_count: int, elapsed: float) -> bool:
        if elapsed >= getattr(settings, "HARD_CUTOFF_MIN", 25):
            return True
        if q_count >= MAX_QUESTIONS:
            return True
        return False