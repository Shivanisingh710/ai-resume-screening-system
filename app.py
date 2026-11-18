import streamlit as st
import pandas as pd
from typing import TypedDict
import json

from langchain_google_genai import (
    ChatGoogleGenerativeAI,
    GoogleGenerativeAIEmbeddings
)

from langchain_core.documents import Document
from langgraph.graph import StateGraph, END
from langchain_community.vectorstores import Chroma
from pypdf import PdfReader

###### config

st.set_page_config(
    page_title="Multi-Agent Resume Screening",
    layout="wide"
)

api_key = st.sidebar.text_input(
    "Enter Google Gemini API Key",
    type="password"
)

if not api_key:
    st.warning("Please enter your Google Gemini API Key to continue.")
    st.stop()

llm = ChatGoogleGenerativeAI(
    model="gemini-2.5-flash",
    temperature=0,
    google_api_key=api_key
)

embeddings = GoogleGenerativeAIEmbeddings(
    model="gemini-embedding-001",
    google_api_key=api_key
)

CHROMA_DIR = "chroma_db"



###### Utilities - Read PDF

def read_pdf(file):
    reader = PdfReader(file)
    return "\n".join(
        [p.extract_text() for p in reader.pages if p.extract_text()]
    )

###### LangGraph State

class ResumeState(TypedDict):
    resume_text: str
    jd_text: str
    candidate_name: str
    parsed_resume: str
    jd_analysis: str
    matched_profile: str

    skills_score: int
    experience_score: int
    projects_score: int
    final_score: int

    is_rejected: bool
    summary: str

##### Building Agents

def resume_parser(state: ResumeState):
    parsed = llm.invoke(
        f"Extract skills, experience, and projects:\n{state['resume_text']}"
    ).content

    name = llm.invoke(
        f"Extract candidate name:\n{state['resume_text']}"
    ).content.strip()

    return {
        "parsed_resume": parsed,
        "candidate_name": name
    }

def jd_analyzer(state: ResumeState):
    jd_analysis = llm.invoke(
        f"Analyze the job description:\n{state['jd_text']}"
    ).content

    return {"jd_analysis": jd_analysis}


def skill_matcher(state: ResumeState):
    matched = llm.invoke(
        f"""
        Resume:
        {state['parsed_resume']}

        Job Description:
        {state['jd_analysis']}
        """
    ).content

    return {"matched_profile": matched}


def scoring_agent(state: ResumeState):
    prompt = f"""
You are a resume evaluation engine.

Evaluate the candidate strictly against the Job Description.

SCORING CRITERIA (Total = 100):
- Skills Match (0-40)
- Experience Relevance (0-35)
- Project Alignment (0-25)

Rules:
- Penalize missing required skills
- Penalize unrelated experience
- Do NOT give similar scores unless profiles are truly similar
- Be discriminative

INPUT:
Job Description:
{state['jd_analysis']}

Resume:
{state['parsed_resume']}

OUTPUT FORMAT (STRICT JSON ONLY):
{{
  "skills_score": <int>,
  "experience_score": <int>,
  "projects_score": <int>,
  "final_score": <int>
}}
"""
    
    response = llm.invoke(prompt).content.strip()

    try:
        scores = json.loads(response)
    except Exception:
        scores = {
            "skills_score": 0,
            "experience_score": 0,
            "projects_score": 0,
            "final_score": 0
        }

    final_score = int(scores["final_score"])

    return {
        "skills_score": int(scores["skills_score"]),
        "experience_score": int(scores["experience_score"]),
        "projects_score": int(scores["projects_score"]),
        "final_score": final_score,
        "is_rejected": final_score < 60
    }


def tie_breaker_agent(tied_resumes: list):
    """
    Tie-breaking priority:
    1. Higher skills_score
    2. Higher experience_score
    """

    sorted_resumes = sorted(
        tied_resumes,
        key=lambda r: (
            r["skills_score"],
            r["experience_score"]
        ),
        reverse=True
    )

    return sorted_resumes[0]

def summarizer_agent(state: ResumeState):
    summary = llm.invoke(
        f"""
        Generate a concise summary including:
        - Skills
        - Experience
        - Projects
        - Pros
        - Cons

        Resume:
        {state['parsed_resume']}
        """
    ).content

    return {"summary": summary}

def gap_analyzer_agent(resume_text, jd_text):
    prompt = f"""
    Identify even minor gaps between the Job Description and Resume.

    JD:
    {jd_text}

    Resume:
    {resume_text}
    """
    return llm.invoke(prompt).content

####### LANGGRAPH DEFINITION

graph = StateGraph(ResumeState)

graph.add_node("parse_resume", resume_parser)
graph.add_node("analyze_jd", jd_analyzer)
graph.add_node("match_profile", skill_matcher)
graph.add_node("score_resume", scoring_agent)
graph.add_node("summarize", summarizer_agent)

graph.set_entry_point("parse_resume")

graph.add_edge("parse_resume", "analyze_jd")
graph.add_edge("analyze_jd", "match_profile")
graph.add_edge("match_profile", "score_resume")

graph.add_conditional_edges(
    "score_resume",
    lambda s: "reject" if s["is_rejected"] else "summarize",
    {
        "reject": END,
        "summarize": "summarize"
    }
)

graph.add_edge("summarize", END)

resume_graph = graph.compile()


##### STREAMLIT UI





st.title("🧠 Multi-Agent Resume Screening (LangGraph + HITL)")

jd_text = st.text_area("📄 Job Description")

uploaded_files = st.file_uploader(
    "📎 Upload Multiple Resumes (PDF)",
    type=["pdf"],
    accept_multiple_files=True
)

if not api_key:
    st.write('Please enter API Key to proceed')

if st.button("🚀 Evaluate Resumes") and uploaded_files and jd_text:
    st.session_state.results = []

    st.session_state.vectorstore = Chroma(
        persist_directory=CHROMA_DIR,
        embedding_function=embeddings
    )

    for file in uploaded_files:
        resume_text = read_pdf(file)

        state = {
            "resume_text": resume_text,
            "jd_text": jd_text
        }

        result = resume_graph.invoke(state)

        if not result["is_rejected"]:
            st.session_state.vectorstore.add_documents([
                Document(
                    page_content=resume_text,
                    metadata={"candidate": result["candidate_name"]}
                )
            ])

        st.session_state.results.append(result)

###### RESULTS DISPLAY

if "results" in st.session_state:
    qualified = [
        r for r in st.session_state.results
        if not r["is_rejected"]
    ]

    if qualified:
        st.subheader("📊 Summary Table (Score ≥ 60)")

        df = pd.DataFrame([
            {
                "Applicant": r["candidate_name"],
                "Score": r["final_score"]
            }
            for r in qualified
        ])
        st.dataframe(df)


        max_score = max(r["final_score"] for r in qualified)

        tied = [r for r in qualified if r["final_score"] == max_score]

        if len(tied) == 1:
            best_candidate = tied[0]
        else:
            best_candidate = tie_breaker_agent(tied)


        st.info(
            f"🏆 Highest AI Scoring Candidate: {best_candidate['candidate_name']}"
        )

        st.subheader("📄 Resume Summaries")
        for r in qualified:
            with st.expander(r["candidate_name"]):
                st.markdown("### 📊 Score Breakdown")
                st.write(f"**Skills Score:** {r['skills_score']} / 40")
                st.write(f"**Experience Score:** {r['experience_score']} / 35")
                st.write(f"**Projects Score:** {r['projects_score']} / 25")
                st.write(f"**Final Score:** {r['final_score']} / 100")

                st.markdown("---")
                st.markdown("### 📝 Resume Summary")
                st.write(r["summary"])

        
        ####### RAG CHAT
       
        st.subheader("💬 Chat with Shortlisted Resumes (RAG)")
        query = st.text_input("Ask a question about shortlisted resumes")

        if query:
            retriever = st.session_state.vectorstore.as_retriever()
            docs = retriever.invoke(query)
            context = "\n".join(d.page_content[:1500] for d in docs)

            answer = llm.invoke(
                f"Context:\n{context}\n\nQuestion:\n{query}"
            )
            st.write(answer.content)

        
        ###### HUMAN-IN-THE-LOOP
        
        st.subheader("👤 Human-in-the-Loop Decision")

        selected = st.multiselect(
            "Select Final Candidate(s)",
            [r["candidate_name"] for r in qualified]
        )

        reject_all = st.checkbox("Reject All")
        on_hold = st.checkbox("Put on Hold")

        if st.button("✅ Finalize Decision"):
            if selected:
                st.success(f"Selected: {', '.join(selected)}")
                for r in qualified:
                    if r["candidate_name"] in selected:
                        gap = gap_analyzer_agent(
                            r["resume_text"], jd_text
                        )
                        st.subheader(
                            f"🔍 Gap Analysis: {r['candidate_name']}"
                        )
                        st.write(gap)
            elif reject_all:
                st.error("All candidates rejected.")
            elif on_hold:
                st.warning("Candidates put on hold.")
    else:
        st.warning("No resumes scored above 60.")
