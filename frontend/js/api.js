// Backend API: base URL, the credentials-including fetch wrapper, endpoint URLs and JSON request helpers.
// Classic script (shared global scope), loaded by index.html before app.js.

const API_BASE_URL = (
  window.APP_CONFIG?.API_BASE_URL ||
  localStorage.getItem("API_BASE_URL") ||
  ""
).replace(/\/$/, "");

const apiUrl = (path) => `${API_BASE_URL}${path}`;

const originalFetch = window.fetch.bind(window);

window.fetch = (input, init = {}) => originalFetch(input, { credentials: "include", ...init });

const AUTH_ME_API_URL = apiUrl("/api/auth/me");

const AUTH_LOGIN_API_URL = apiUrl("/api/auth/login");

const AUTH_SIGNUP_API_URL = apiUrl("/api/auth/signup");

const AUTH_LOGOUT_API_URL = apiUrl("/api/auth/logout");

const CONVERSATIONS_API_URL = apiUrl("/api/conversations");

const SOURCES_API_URL = apiUrl("/api/sources");

const UPLOAD_API_URL = apiUrl("/api/sources/upload");

const DELETE_SOURCE_API_URL = apiUrl("/api/sources");

const DOCUMENTS_API_URL = apiUrl("/api/documents");

const QUIZZES_API_URL = apiUrl("/api/quizzes");

const QUIZ_API_BASE_URL = apiUrl("/api/quiz");

const QUIZ_GENERATE_API_URL = apiUrl("/api/quiz/generate");

const QUIZ_HISTORY_API_URL = apiUrl("/api/quiz-history");

const DASHBOARD_API_URL = apiUrl("/api/dashboard");

const KNOWLEDGE_GAPS_API_URL = apiUrl("/api/knowledge-gaps");

const RECOMMENDATIONS_API_URL = apiUrl("/api/recommendations");

const MODELS_API_URL = apiUrl("/api/models");

const SUMMARY_API_BASE_URL = apiUrl("/api/summary");

const FLASHCARDS_API_BASE_URL = apiUrl("/api/flashcards");

const PLANNER_AVAILABILITY_API_URL = apiUrl("/api/planner/availability");

const PLANNER_PLANS_API_URL = apiUrl("/api/planner/plans");

const PLANNER_SESSIONS_API_URL = apiUrl("/api/planner/sessions");

const ADMIN_QUIZ_MODEL_COMPARISON_API_URL = apiUrl("/api/admin/quiz-model-comparison");

const PROGRESS_API_BASE_URL = apiUrl("/api/progress/documents");

async function fetchJson(url, options = {}) {
  const response = await fetch(url, options);
  if (!response.ok) {
    let detail = `Request returned ${response.status}`;
    try {
      detail = (await response.json()).detail || detail;
    } catch (error) {
      // Keep the HTTP status for non-JSON responses.
    }
    if (response.status === 401 && currentUser) showAuthentication();
    throw new Error(detail);
  }
  return response.json();
}
