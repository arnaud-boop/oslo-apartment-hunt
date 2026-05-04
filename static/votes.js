/* Oslo Apartment Hunt — voting client.
 *
 * Reads ?v= from the URL ("arnaud" or "celine") to identify the voter, then:
 *   - On page load, GETs the latest vote state from the Apps Script Web App
 *     and applies it to all cards. (The page is also pre-rendered with the
 *     state from build time, so this update covers the gap since last build.)
 *   - On click of vote buttons / note save, POSTs the change to the Web App
 *     with optimistic UI updates.
 *
 * If ?v= is missing, the page is read-only — buttons remain visible (so you
 * can see both partners' state) but disabled.
 */
(function () {
  "use strict";

  const PARAMS = new URLSearchParams(window.location.search);
  const VOTER = (PARAMS.get("v") || "").toLowerCase();
  const ENDPOINT = window.VOTING_ENDPOINT || "";

  const isInteractive = (VOTER === "arnaud" || VOTER === "celine") && ENDPOINT;

  // Propagate ?v= to all internal navigation links so the voter identity
  // carries from the digest into the eval pages and back. Marked links are
  // anything with class="eval-link" or class="eval-back".
  if (VOTER) {
    const propagate = function (a) {
      try {
        const url = new URL(a.href, window.location.href);
        // Only rewrite same-origin links — leave external (Finn) untouched.
        if (url.origin !== window.location.origin) return;
        url.searchParams.set("v", VOTER);
        a.href = url.toString();
      } catch (e) { /* malformed href, leave alone */ }
    };
    document.querySelectorAll("a.eval-link, a.eval-back").forEach(propagate);
  }

  // Mark the page so CSS knows whether we have a voter and an endpoint.
  document.body.dataset.voter = isInteractive ? VOTER : "";
  document.body.dataset.votingActive = isInteractive ? "1" : "";

  if (isInteractive) {
    fetchAllVotes();
    document.querySelectorAll("[data-vote-button]").forEach(function (btn) {
      btn.addEventListener("click", onVoteClick);
    });
    document.querySelectorAll("[data-note-toggle]").forEach(function (btn) {
      btn.addEventListener("click", onNoteToggle);
    });
    document.querySelectorAll("[data-note-save]").forEach(function (btn) {
      btn.addEventListener("click", onNoteSave);
    });
    document.querySelectorAll("[data-note-cancel]").forEach(function (btn) {
      btn.addEventListener("click", onNoteCancel);
    });
  }

  // Banner count is server-rendered as the collective "new in batch"
  // figure. Recompute it once now to reflect the current voter's
  // acknowledged state (e.g. 5 new total but you've already acked 2 → 3).
  refreshBannerCount();

  // Photo carousels (eval pages only — querySelector finds nothing on the
  // digest, so this is a safe no-op there).
  initCarousels();

  function initCarousels() {
    document.querySelectorAll("[data-carousel]").forEach(function (strip) {
      const dotsContainer = strip.parentElement.querySelector("[data-carousel-dots]");
      const slides = Array.prototype.slice.call(strip.querySelectorAll(".photo-slide"));
      const dots = dotsContainer
        ? Array.prototype.slice.call(dotsContainer.querySelectorAll(".photo-dot"))
        : [];
      if (slides.length < 2) return;

      // Click a dot → smooth-scroll the strip to align that slide.
      dots.forEach(function (dot, i) {
        dot.addEventListener("click", function () {
          const slide = slides[i];
          if (!slide) return;
          // Use scrollLeft instead of scrollIntoView, since scrollIntoView
          // can scroll the page itself when the strip is partially off-screen.
          strip.scrollTo({ left: slide.offsetLeft - strip.offsetLeft, behavior: "smooth" });
        });
      });

      // Watch which slide is mostly visible; mark the corresponding dot active.
      if ("IntersectionObserver" in window) {
        const io = new IntersectionObserver(
          function (entries) {
            entries.forEach(function (entry) {
              if (entry.intersectionRatio < 0.55) return;
              const idx = slides.indexOf(entry.target);
              if (idx < 0) return;
              dots.forEach(function (d, i) {
                d.classList.toggle("active", i === idx);
              });
            });
          },
          { root: strip, threshold: [0.55, 0.75] }
        );
        slides.forEach(function (s) { io.observe(s); });
      }
    });
  }

  function fetchAllVotes() {
    fetch(ENDPOINT, { method: "GET", cache: "no-cache" })
      .then(function (r) {
        if (!r.ok) throw new Error("HTTP " + r.status);
        return r.json();
      })
      .then(function (data) {
        applyVotes(data.votes || []);
      })
      .catch(function (e) {
        console.warn("[votes] failed to fetch:", e);
      });
  }

  function applyVotes(rows) {
    rows.forEach(function (row) {
      const finn = String(row.finn_id || "");
      const voter = String(row.voter || "").toLowerCase();
      if (!finn || !voter) return;
      const card = document.querySelector(
        'article[data-finn-id="' + cssEscape(finn) + '"]'
      );
      if (!card) return;
      const column = card.querySelector('.vote-column[data-voter="' + voter + '"]');
      if (column) setColumnState(column, row.vote || "", row.note || "");
      setAckedAttr(card, voter, row.vote, row.note);
    });
    refreshBannerCount();
  }

  function setAckedAttr(card, voter, vote, note) {
    const acked = (vote && vote !== "") || (note && note !== "");
    const attr = "data-" + voter + "-acked";
    if (acked) card.setAttribute(attr, "1");
    else card.removeAttribute(attr);
  }

  function refreshBannerCount() {
    if (!VOTER) return;
    const banner = document.querySelector(".new-arrivals-banner");
    if (!banner) return;
    const counter = banner.querySelector("[data-new-banner-count]");
    const suffix = banner.querySelector("[data-new-banner-suffix]");
    // Count cards that are new-in-batch AND not acked by current voter.
    const cards = document.querySelectorAll(
      'article[data-new-in-batch="1"]:not([data-' + VOTER + '-acked="1"])'
    );
    if (counter) counter.textContent = String(cards.length);
    if (suffix) suffix.textContent = " (for you)";
    if (cards.length === 0) banner.style.display = "none";
    else banner.style.display = "";
  }

  function setColumnState(column, vote, note) {
    column.querySelectorAll("[data-vote-button]").forEach(function (btn) {
      btn.classList.toggle("active", btn.dataset.voteButton === vote);
    });
    const display = column.querySelector(".note-display");
    if (display) {
      display.textContent = note || "";
      display.classList.toggle("empty", !note);
    }
    // Pre-fill the textarea (only the active voter's column has one).
    const textarea = column.querySelector("textarea[data-note-input]");
    if (textarea) textarea.value = note || "";
  }

  function onVoteClick(e) {
    const btn = e.currentTarget;
    const card = btn.closest("[data-finn-id]");
    if (!card) return;
    const column = btn.closest(".vote-column");
    if (!column || column.dataset.voter !== VOTER) return; // read-only column
    const finnId = card.dataset.finnId;
    const value = btn.dataset.voteButton; // "up" or "down"
    const newVote = btn.classList.contains("active") ? "" : value;

    // Optimistic update.
    column.querySelectorAll("[data-vote-button]").forEach(function (b) {
      b.classList.toggle("active", b.dataset.voteButton === newVote);
    });
    setStatus(card, "pending");
    postVote({ finn_id: finnId, voter: VOTER, vote: newVote })
      .then(function (data) {
        setStatus(card, "ok");
        // Server response carries the resulting (vote, note) — use it to
        // sync the acked attribute (which controls the 🆕 NEW tag visibility).
        setAckedAttr(
          card,
          VOTER,
          (data && data.vote) || newVote,
          (data && data.note) || ""
        );
        refreshBannerCount();
      })
      .catch(function (err) {
        console.warn("[votes] vote POST failed:", err);
        setStatus(card, "err");
      });
  }

  function onNoteToggle(e) {
    const card = e.currentTarget.closest("[data-finn-id]");
    if (!card) return;
    const column = e.currentTarget.closest(".vote-column");
    if (!column || column.dataset.voter !== VOTER) return; // read-only
    const editor = card.querySelector(".note-editor");
    if (!editor) return;
    editor.classList.toggle("expanded");
    if (editor.classList.contains("expanded")) {
      const ta = editor.querySelector("textarea");
      if (ta) ta.focus();
    }
  }

  function onNoteSave(e) {
    const card = e.currentTarget.closest("[data-finn-id]");
    if (!card) return;
    const finnId = card.dataset.finnId;
    const editor = card.querySelector(".note-editor");
    const ta = editor && editor.querySelector("textarea");
    if (!ta) return;
    const note = ta.value;
    const column = card.querySelector('.vote-column[data-voter="' + VOTER + '"]');

    setStatus(card, "pending");
    postVote({ finn_id: finnId, voter: VOTER, note: note })
      .then(function (data) {
        if (column) {
          const display = column.querySelector(".note-display");
          if (display) {
            display.textContent = note || "";
            display.classList.toggle("empty", !note);
          }
        }
        editor.classList.remove("expanded");
        setStatus(card, "ok");
        setAckedAttr(
          card,
          VOTER,
          (data && data.vote) || "",
          (data && data.note) || note || ""
        );
        refreshBannerCount();
      })
      .catch(function (err) {
        console.warn("[votes] note POST failed:", err);
        setStatus(card, "err");
      });
  }

  function onNoteCancel(e) {
    const card = e.currentTarget.closest("[data-finn-id]");
    if (!card) return;
    const editor = card.querySelector(".note-editor");
    if (!editor) return;
    // Restore textarea from the displayed note.
    const column = card.querySelector('.vote-column[data-voter="' + VOTER + '"]');
    const display = column && column.querySelector(".note-display");
    const ta = editor.querySelector("textarea");
    if (display && ta) ta.value = display.textContent || "";
    editor.classList.remove("expanded");
  }

  function postVote(body) {
    if (!ENDPOINT) return Promise.reject(new Error("no endpoint"));
    return fetch(ENDPOINT, {
      method: "POST",
      // text/plain avoids the CORS preflight that Apps Script can't handle.
      headers: { "Content-Type": "text/plain;charset=utf-8" },
      body: JSON.stringify(body),
    }).then(function (r) {
      if (!r.ok) throw new Error("HTTP " + r.status);
      return r.json();
    }).then(function (data) {
      if (data && data.ok === false) {
        throw new Error(data.error || "server error");
      }
      return data;
    });
  }

  function setStatus(card, state) {
    const dot = card.querySelector(".vote-status");
    if (!dot) return;
    dot.classList.remove("pending", "ok", "err");
    if (state) dot.classList.add(state);
    if (state === "ok" || state === "err") {
      setTimeout(function () { dot.classList.remove(state); }, 2000);
    }
  }

  // Minimal CSS.escape polyfill for older browsers (modern macOS browsers
  // support CSS.escape natively; this is just a fallback).
  function cssEscape(s) {
    if (window.CSS && typeof CSS.escape === "function") return CSS.escape(s);
    return String(s).replace(/[^\w-]/g, function (c) {
      return "\\" + c;
    });
  }
})();
