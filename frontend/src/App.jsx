import { useEffect, useState } from "react";
import {
  BrowserRouter,
  Link,
  Navigate,
  Route,
  Routes,
  useLocation,
  useNavigate,
  useParams,
} from "react-router-dom";
import "./App.css";

const API_BASE = (
  import.meta.env.VITE_API_BASE || "https://ewaa-api.taytanium.com"
).replace(/\/+$/, "");

const TOKEN_KEY = "ewaa-admin-token";

function readToken() {
  try {
    return localStorage.getItem(TOKEN_KEY) || "";
  } catch {
    return "";
  }
}

function saveToken(token) {
  try {
    if (token) {
      localStorage.setItem(TOKEN_KEY, token);
    } else {
      localStorage.removeItem(TOKEN_KEY);
    }
  } catch {
    // Authentication still works for this tab if storage is unavailable.
  }
}

async function api(
  path,
  { method = "GET", body, token, signal } = {}
) {
  const headers = { Accept: "application/json" };

  if (body !== undefined) {
    headers["Content-Type"] = "application/json";
  }

  if (token) {
    headers.Authorization = `Bearer ${token}`;
  }

  const response = await fetch(`${API_BASE}${path}`, {
    method,
    headers,
    body: body === undefined ? undefined : JSON.stringify(body),
    signal,
    credentials: "omit",
    cache: "no-store",
  });

  const data = await response.json().catch(() => ({}));

  if (!response.ok) {
    const error = new Error(
      data.error || `Request failed (${response.status}).`
    );
    error.status = response.status;
    throw error;
  }

  return data;
}

function wordPath(word) {
  return `/word/${encodeURIComponent(word.toLowerCase())}/`;
}

function isValidWord(word) {
  return /^[a-z]{1,40}$/i.test(word);
}

function titleCase(word) {
  return word.charAt(0).toUpperCase() + word.slice(1);
}

function Arrow({ diagonal = false }) {
  return (
    <svg
      className="arrow"
      width="18"
      height="18"
      viewBox="0 0 24 24"
      fill="none"
      stroke="currentColor"
      strokeWidth="1.7"
      strokeLinecap="round"
      strokeLinejoin="round"
      aria-hidden="true"
    >
      {diagonal ? (
        <path d="M6 18 18 6M6 6h12v12" />
      ) : (
        <path d="M5 12h14m-6-6 6 6-6 6" />
      )}
    </svg>
  );
}

function ShuffleIcon() {
  return (
    <svg
      width="18"
      height="18"
      viewBox="0 0 24 24"
      fill="none"
      stroke="currentColor"
      strokeWidth="1.7"
      strokeLinecap="round"
      strokeLinejoin="round"
      aria-hidden="true"
    >
      <path d="M3 6h3c4 0 8 12 12 12h3m-4-4 4 4-4 4" />
      <path d="M3 18h3c1.7 0 3.3-2.2 5-5M14 8c1.4-1.4 2.7-2 4-2h3m-4-4 4 4-4 4" />
    </svg>
  );
}

function Message({ children, error = false }) {
  if (!children) return null;

  return (
    <p
      className={`message ${error ? "message-error" : "message-success"}`}
      role={error ? "alert" : "status"}
    >
      {children}
    </p>
  );
}

function RandomButton({
  className = "button button-secondary",
  children = "Random word",
}) {
  const navigate = useNavigate();
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");

  async function chooseRandom() {
    setBusy(true);
    setError("");

    try {
      const data = await api("/api/random");
      navigate(wordPath(data.word));
    } catch (err) {
      setError(err.message);
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="random-control">
      <button
        className={className}
        type="button"
        onClick={chooseRandom}
        disabled={busy}
      >
        <ShuffleIcon />
        {busy ? "Finding a word…" : children}
      </button>
      {error && <Message error>{error}</Message>}
    </div>
  );
}

function SearchForm({ initialValue = "", compact = false }) {
  const navigate = useNavigate();
  const [value, setValue] = useState(initialValue);
  const [error, setError] = useState("");

  function submit(event) {
    event.preventDefault();
    const word = value.trim().toLowerCase();

    if (!isValidWord(word)) {
      setError("Enter one word using 1–40 English letters.");
      return;
    }

    setError("");
    navigate(wordPath(word));
  }

  return (
    <form
      className={`search-form ${compact ? "search-form-compact" : ""}`}
      onSubmit={submit}
      role="search"
      aria-label="Look up a word"
    >
      <label className="search-label">
        <span className="sr-only">Word to look up</span>
        <input
          type="search"
          value={value}
          onChange={(event) => setValue(event.target.value)}
          placeholder="Enter a word, like marker"
          maxLength={40}
          autoComplete="off"
          autoCapitalize="none"
          spellCheck={false}
          aria-invalid={Boolean(error)}
          required
        />
      </label>

      <button className="button button-primary" type="submit">
        Look up <Arrow />
      </button>

      {error && (
        <div className="search-error">
          <Message error>{error}</Message>
        </div>
      )}
    </form>
  );
}

function RouteEffects() {
  const { pathname } = useLocation();

  useEffect(() => {
    window.scrollTo({ top: 0, behavior: "instant" });
    document.getElementById("main-content")?.focus({
      preventScroll: true,
    });
  }, [pathname]);

  return null;
}

function Layout({ children }) {
  return (
    <div className="site">
      <a className="skip-link" href="#main-content">
        Skip to content
      </a>

      <header className="site-header">
        <div className="container header-inner">
          <Link className="brand" to="/" aria-label="EWAA home">
            <span className="brand-symbol" aria-hidden="true">
              E
            </span>
            <span className="brand-name">EWAA</span>
          </Link>

          <nav className="site-nav" aria-label="Main navigation">
            <Link to="/">Explore</Link>
            <a
              href="https://taytanium.com"
              target="_blank"
              rel="noreferrer"
            >
              TayTanium <Arrow diagonal />
            </a>
          </nav>
        </div>
      </header>

      <main
        id="main-content"
        className="container main-content"
        tabIndex={-1}
      >
        {children}
      </main>

      <footer className="site-footer">
        <div className="container footer-inner">
          <div>
            <Link className="footer-title" to="/">
              EWAA
            </Link>
            <p>Every Word&apos;s An Acronym.</p>
          </div>

          <div className="footer-links">
            <Link to="/issues">Issue dashboard</Link>
            <a href="https://taytanium.com">
              Created by TayTanium Software <Arrow diagonal />
            </a>
          </div>
        </div>

        <div className="container footer-bottom">
          <span>
            © {new Date().getFullYear()} TayTanium Software
          </span>
          <span>Words within words.</span>
        </div>
      </footer>
    </div>
  );
}

function HomePage() {
  const [count, setCount] = useState(null);

  useEffect(() => {
    document.title = "EWAA — Every Word's An Acronym";
    const controller = new AbortController();

    api("/api/stats", { signal: controller.signal })
      .then((data) => setCount(data.words))
      .catch(() => {});

    return () => controller.abort();
  }, []);

  return (
    <>
      <section className="hero">
        <div className="hero-copy">
          <div className="pill">
            <span className="pill-dot" />
            A TayTanium Software project
          </div>

          <h1>
            Every word.
            <br />
            <span className="hero-muted">Another meaning.</span>
          </h1>

          <p className="lead hero-lead">
            EWAA stands for <strong>Every Word&apos;s An Acronym.</strong>{" "}
            We turn words into phrases that describe what they mean,
            using every letter along the way.
          </p>

          <div className="hero-search">
            <SearchForm />
            <div className="hero-search-bottom">
              <RandomButton />
              {count !== null && (
                <span className="dictionary-count">
                  {count.toLocaleString()}{" "}
                  {count === 1 ? "word" : "words"} and growing
                </span>
              )}
            </div>
          </div>
        </div>

        <div className="example-card">
          <div className="example-top">
            <span className="eyebrow">One word. Three more.</span>
            <span className="mini-tag">Example</span>
          </div>

          <Link className="example-word" to="/word/yum/">
            YUM <Arrow diagonal />
          </Link>

          <div className="example-parts">
            {["yields", "ultimate", "mouthpleasure"].map((part) => (
              <Link
                key={part}
                className="example-part"
                to={wordPath(part)}
              >
                <span className="example-letter">
                  {part.charAt(0).toUpperCase()}
                </span>
                <span>{titleCase(part)}</span>
                <Arrow />
              </Link>
            ))}
          </div>

          <p>
            Every word in an acronym is a link.
            <br />
            See where the next one takes you.
          </p>
        </div>
      </section>

      <section className="home-section" aria-labelledby="how-title">
        <div className="section-heading">
          <div>
            <p className="eyebrow">The idea</p>
            <h2 id="how-title">A dictionary with a different definition.</h2>
          </div>
          <p>A little language. A little creativity. A lot of connections.</p>
        </div>

        <div className="values">
          <article className="value">
            <span className="value-number">01 / START</span>
            <h3>Choose a word</h3>
            <p>
              Search for something familiar, or let the random button
              choose your starting point.
            </p>
          </article>

          <article className="value">
            <span className="value-number">02 / UNPACK</span>
            <h3>Read between the letters</h3>
            <p>
              Each letter starts a word in a phrase that aims to describe
              the original word.
            </p>
          </article>

          <article className="value">
            <span className="value-number">03 / FOLLOW</span>
            <h3>Keep exploring</h3>
            <p>
              Follow any word in the phrase to its own page. Something
              missing? Help us fill the gap.
            </p>
          </article>
        </div>
      </section>

      <section className="about-card">
        <div>
          <p className="eyebrow">A work in progress</p>
          <h2>Playful definitions. Not perfect synonyms.</h2>
          <p>
            These are creatively constructed acronyms, sometimes called
            backronyms—not the historical origins of the words. The
            dictionary is growing, and your reports help improve it.
          </p>
        </div>
        <RandomButton />
      </section>
    </>
  );
}

function IssueReport({ word }) {
  const [open, setOpen] = useState(false);
  const [details, setDetails] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [success, setSuccess] = useState("");

  async function submit(event) {
    event.preventDefault();
    setBusy(true);
    setError("");

    try {
      await api("/api/reports", {
        method: "POST",
        body: {
          kind: "acronym",
          word,
          details: details.trim(),
        },
      });

      setSuccess("Thanks! Your report has been sent.");
      setOpen(false);
      setDetails("");
    } catch (err) {
      setError(err.message);
    } finally {
      setBusy(false);
    }
  }

  return (
    <section className="report-section" aria-label="Report an issue">
      <div className="report-heading">
        <div>
          <h2>Something not quite right?</h2>
          <p>
            Tell us if the phrase could describe this word better.
          </p>
        </div>

        {!success && (
          <button
            className="button button-secondary"
            type="button"
            aria-expanded={open}
            onClick={() => setOpen((current) => !current)}
          >
            {open ? "Cancel" : "Report an issue"}
          </button>
        )}
      </div>

      <Message>{success}</Message>

      {open && (
        <form className="report-form" onSubmit={submit}>
          <label htmlFor="issue-details">
            What should we improve?
          </label>
          <textarea
            id="issue-details"
            value={details}
            onChange={(event) => setDetails(event.target.value)}
            minLength={10}
            maxLength={2000}
            rows={4}
            placeholder="Explain the problem, or suggest a better acronym…"
            required
          />
          <div className="form-bottom">
            <span className="form-hint">
              Please don&apos;t include personal information.
            </span>
            <button
              className="button button-primary"
              disabled={busy}
              type="submit"
            >
              {busy ? "Sending…" : "Send report"} <Arrow />
            </button>
          </div>
          <Message error>{error}</Message>
        </form>
      )}
    </section>
  );
}

function MissingWord({ word }) {
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [success, setSuccess] = useState("");
  const valid = isValidWord(word);

  async function reportMissing() {
    setBusy(true);
    setError("");

    try {
      const data = await api("/api/reports", {
        method: "POST",
        body: { kind: "missing", word },
      });
      setSuccess(data.message);
    } catch (err) {
      setError(err.message);
    } finally {
      setBusy(false);
    }
  }

  return (
    <section className="empty-state">
      <p className="eyebrow">An undiscovered word</p>
      <h1>
        We haven&apos;t unpacked
        <br />
        <span className="accent-text">“{word}”</span> yet.
      </h1>
      <p className="lead">
        {valid
          ? "There isn’t an acronym for this word in our dictionary yet. Report it below, or try another word."
          : "Search using one word containing 1–40 English letters, without spaces or punctuation."}
      </p>

      <div className="empty-search">
        <SearchForm />
      </div>

      <div className="actions">
        <RandomButton />
        <Link className="button button-secondary" to="/">
          Back to home
        </Link>
        {valid && (
          <button
            className="button button-primary"
            onClick={reportMissing}
            disabled={busy || Boolean(success)}
            type="button"
          >
            {success
              ? "Missing word reported"
              : busy
                ? "Sending…"
                : "Report missing word"}
          </button>
        )}
      </div>

      <Message>{success}</Message>
      <Message error>{error}</Message>
    </section>
  );
}

function WordPage() {
  const { word: rawWord = "" } = useParams();
  const word = rawWord.toLowerCase();

  if (rawWord !== word) {
    return <Navigate to={wordPath(word)} replace />;
  }

  // Reset page-local state when navigating between words.
  return <WordContent key={word} word={word} />;
}

function WordContent({ word }) {
  const [state, setState] = useState({
    loading: true,
    data: null,
    missing: false,
    error: "",
  });
  const [retry, setRetry] = useState(0);

  useEffect(() => {
    document.title = `${word.toUpperCase()} — EWAA`;

    if (!isValidWord(word)) {
      setState({
        loading: false,
        data: null,
        missing: true,
        error: "",
      });
      return;
    }

    const controller = new AbortController();

    setState({
      loading: true,
      data: null,
      missing: false,
      error: "",
    });

    api(`/api/words/${encodeURIComponent(word)}`, {
      signal: controller.signal,
    })
      .then((data) => {
        setState({
          loading: false,
          data,
          missing: false,
          error: "",
        });
      })
      .catch((err) => {
        if (err.name === "AbortError") return;

        setState({
          loading: false,
          data: null,
          missing: err.status === 404,
          error: err.status === 404 ? "" : err.message,
        });
      });

    return () => controller.abort();
  }, [word, retry]);

  return (
    <div className="page word-page">
      <div className="page-toolbar">
        <Link className="back-link" to="/">
          <span aria-hidden="true">←</span> All possibilities
        </Link>
        <RandomButton />
      </div>

      {state.loading ? (
        <div className="loading-panel" role="status">
          Unpacking the letters…
        </div>
      ) : state.missing ? (
        <MissingWord word={word} />
      ) : state.error ? (
        <section className="error-panel">
          <h1>We couldn&apos;t load this word.</h1>
          <Message error>{state.error}</Message>
          <button
            className="button button-primary"
            onClick={() => setRetry((value) => value + 1)}
          >
            Try again
          </button>
        </section>
      ) : (
        <>
          <header className="word-header">
            <div className="word-meta">
              <span className="eyebrow eyebrow-accent">
                Every letter has a part to play
              </span>
              <span className="mini-tag">
                {state.data.word.length} letters
              </span>
            </div>

            <h1>{state.data.word.toUpperCase()}</h1>

            <p className="phrase">
              {state.data.expansion.map((part, index) => (
                <span key={`${index}-${part}`}>
                  {index > 0 && " "}
                  <Link to={wordPath(part)} className="phrase-word">
                    {titleCase(part)}
                  </Link>
                </span>
              ))}
            </p>
          </header>

          <section
            className="acronym-section"
            aria-labelledby="acronym-title"
          >
            <div className="section-heading">
              <div>
                <p className="eyebrow">The acronym</p>
                <h2 id="acronym-title">One letter at a time.</h2>
              </div>
              <p>Click any word to follow the connection.</p>
            </div>

            <ol className="acronym-list">
              {state.data.expansion.map((part, index) => (
                <li key={`${index}-${part}`}>
                  <Link className="acronym-row" to={wordPath(part)}>
                    <span className="letter-tile" aria-hidden="true">
                      {word[index].toUpperCase()}
                    </span>
                    <span className="acronym-part">
                      {titleCase(part)}
                    </span>
                    <span className="acronym-index" aria-hidden="true">
                      {String(index + 1).padStart(2, "0")}
                    </span>
                    <Arrow />
                  </Link>
                </li>
              ))}
            </ol>
          </section>

          <IssueReport word={word} />

          <section className="continue-section">
            <p className="eyebrow">Keep exploring</p>
            <h2>Another word, another connection.</h2>
            <SearchForm compact />
          </section>
        </>
      )}
    </div>
  );
}

function LoginForm({ onLogin }) {
  const [password, setPassword] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");

  async function submit(event) {
    event.preventDefault();
    setBusy(true);
    setError("");

    try {
      const data = await api("/api/admin/login", {
        method: "POST",
        body: { password },
      });

      setPassword("");
      onLogin(data.token);
    } catch (err) {
      setError(err.message);
    } finally {
      setBusy(false);
    }
  }

  return (
    <section className="login-card">
      <span className="mini-tag">Admin access</span>
      <h1>Issue dashboard</h1>
      <p>
        Sign in to review missing words and acronym reports.
        This browser will remember your login for up to seven days.
      </p>

      <form className="login-form" onSubmit={submit}>
        <label htmlFor="admin-password">Password</label>
        <input
          id="admin-password"
          type="password"
          autoComplete="current-password"
          value={password}
          onChange={(event) => setPassword(event.target.value)}
          maxLength={256}
          required
        />
        <button
          className="button button-primary"
          disabled={busy}
          type="submit"
        >
          {busy ? "Signing in…" : "Sign in"} <Arrow />
        </button>
        <Message error>{error}</Message>
      </form>
    </section>
  );
}

function IssuesPage() {
  const [token, setToken] = useState(readToken);
  const [status, setStatus] = useState("open");
  const [kind, setKind] = useState("all");
  const [page, setPage] = useState(0);
  const [refresh, setRefresh] = useState(0);
  const [data, setData] = useState({ items: [], total: 0 });
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState("");
  const [updating, setUpdating] = useState(null);

  const pageSize = 30;

  useEffect(() => {
    document.title = "Issue dashboard — EWAA";
  }, []);

  function forgetSession() {
    saveToken("");
    setToken("");
    setData({ items: [], total: 0 });
  }

  function login(newToken) {
    saveToken(newToken);
    setToken(newToken);
    setError("");
    setPage(0);
  }

  async function logout() {
    const oldToken = token;
    forgetSession();

    try {
      await api("/api/admin/logout", {
        method: "POST",
        token: oldToken,
      });
    } catch {
      // Local credentials have still been removed.
    }
  }

  useEffect(() => {
    if (!token) return;

    const controller = new AbortController();
    setLoading(true);
    setError("");

    const query = new URLSearchParams({
      status,
      kind,
      limit: String(pageSize),
      offset: String(page * pageSize),
    });

    api(`/api/admin/issues?${query}`, {
      token,
      signal: controller.signal,
    })
      .then(setData)
      .catch((err) => {
        if (err.name === "AbortError") return;

        if (err.status === 401) {
          saveToken("");
          setToken("");
          setData({ items: [], total: 0 });
        } else {
          setError(err.message);
        }
      })
      .finally(() => {
        if (!controller.signal.aborted) setLoading(false);
      });

    return () => controller.abort();
  }, [token, status, kind, page, refresh]);

  async function updateStatus(issue) {
    setUpdating(issue.id);
    setError("");

    try {
      await api(`/api/admin/issues/${issue.id}`, {
        method: "PATCH",
        token,
        body: {
          status: issue.status === "open" ? "resolved" : "open",
        },
      });

      setRefresh((value) => value + 1);
    } catch (err) {
      if (err.status === 401) {
        forgetSession();
      } else {
        setError(err.message);
      }
    } finally {
      setUpdating(null);
    }
  }

  if (!token) {
    return (
      <div className="page">
        <LoginForm onLogin={login} />
      </div>
    );
  }

  return (
    <div className="page admin-page">
      <div className="admin-heading">
        <div>
          <p className="eyebrow eyebrow-accent">Dictionary maintenance</p>
          <h1>Issue dashboard</h1>
        </div>
        <button
          className="button button-secondary"
          type="button"
          onClick={logout}
        >
          Sign out
        </button>
      </div>

      <div className="admin-filters">
        <label>
          Status
          <select
            value={status}
            onChange={(event) => {
              setStatus(event.target.value);
              setPage(0);
            }}
          >
            <option value="open">Open</option>
            <option value="resolved">Resolved</option>
            <option value="all">All statuses</option>
          </select>
        </label>

        <label>
          Report type
          <select
            value={kind}
            onChange={(event) => {
              setKind(event.target.value);
              setPage(0);
            }}
          >
            <option value="all">All reports</option>
            <option value="missing">Missing words</option>
            <option value="acronym">Acronym issues</option>
          </select>
        </label>

        <button
          className="button button-secondary"
          onClick={() => setRefresh((value) => value + 1)}
          disabled={loading}
          type="button"
        >
          Refresh
        </button>
      </div>

      <Message error>{error}</Message>

      {loading ? (
        <div className="loading-panel" role="status">
          Loading reports…
        </div>
      ) : !error ? (
        <>
          <p className="result-count">
            {data.total} matching {data.total === 1 ? "report" : "reports"}
          </p>

          {data.items.length === 0 ? (
            <div className="dashboard-empty">
              No reports here. Try another filter or page.
            </div>
          ) : (
            <div className="issue-list">
              {data.items.map((issue) => (
                <article className="issue-card" key={issue.id}>
                  <div className="issue-top">
                    <div className="issue-tags">
                      <span className="mini-tag">
                        {issue.kind === "missing"
                          ? "Missing word"
                          : "Acronym issue"}
                      </span>
                      <span
                        className={`status-tag status-${issue.status}`}
                      >
                        {issue.status}
                      </span>
                    </div>
                    <span className="issue-number">#{issue.id}</span>
                  </div>

                  <h2>
                    <Link to={wordPath(issue.word)}>
                      {issue.word} <Arrow diagonal />
                    </Link>
                  </h2>

                  {issue.details && (
                    <p className="issue-details">{issue.details}</p>
                  )}

                  <div className="issue-bottom">
                    <time dateTime={issue.created_at}>
                      {new Date(issue.created_at).toLocaleString()}
                    </time>
                    <button
                      className="button button-secondary button-small"
                      type="button"
                      disabled={updating !== null}
                      onClick={() => updateStatus(issue)}
                    >
                      {updating === issue.id
                        ? "Saving…"
                        : issue.status === "open"
                          ? "Mark resolved"
                          : "Reopen"}
                    </button>
                  </div>
                </article>
              ))}
            </div>
          )}

          <div className="pagination">
            <button
              className="button button-secondary"
              disabled={page === 0}
              onClick={() => setPage((value) => value - 1)}
              type="button"
            >
              Previous
            </button>
            <span>Page {page + 1}</span>
            <button
              className="button button-secondary"
              disabled={(page + 1) * pageSize >= data.total}
              onClick={() => setPage((value) => value + 1)}
              type="button"
            >
              Next
            </button>
          </div>
        </>
      ) : null}
    </div>
  );
}

function NotFoundPage() {
  useEffect(() => {
    document.title = "Page not found — EWAA";
  }, []);

  return (
    <section className="empty-state not-found">
      <p className="eyebrow">404 / A wrong turn</p>
      <h1>This page has no definition.</h1>
      <p className="lead">
        The page you&apos;re looking for doesn&apos;t exist.
        Let&apos;s get you back to the words.
      </p>
      <div className="actions">
        <Link className="button button-primary" to="/">
          Back to home <Arrow />
        </Link>
        <RandomButton />
      </div>
    </section>
  );
}

export default function App() {
  return (
    <BrowserRouter>
      <RouteEffects />
      <Layout>
        <Routes>
          <Route path="/" element={<HomePage />} />
          <Route path="/word/:word/" element={<WordPage />} />
          <Route path="/issues" element={<IssuesPage />} />
          <Route path="*" element={<NotFoundPage />} />
        </Routes>
      </Layout>
    </BrowserRouter>
  );
}