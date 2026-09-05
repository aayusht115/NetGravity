/**
 * NetGravity — AI Assistant & Chatbot Controller
 * ===============================================
 * Implements the approved Ask Netgravity UI popup modal:
 * - Floating FAB trigger across all screens
 * - Topbar search pill trigger
 * - Backdrop blur modal overlay
 * - Pre-configured intelligent FAQ answers
 * - Dynamic domain-specific consulting responses
 */

// REMOVED IN PHASE 10.0 — `FAQ_KNOWLEDGE_BASE` and `generateAIResponse()`
//
// Together these were the chatbot's offline fabrication path: a canned FAQ plus
// a generator that, whenever the orchestrator call failed or returned no text,
// emitted a confident business briefing containing specific figures the engine
// had never produced ("96.7% On-time SLA", "Delhi NCR DC 94% utilization",
// "Kolkata DC spare capacity 53.3%", "all 19 India facilities").
//
// They are removed rather than left unreferenced so the path cannot be
// reconnected by accident. Grounded answers come from /orchestrator/chat; when
// that is unreachable the UI says so.


import { getActiveSnapshotId } from './integration/project-context.js';

let chatMessages = [];
let isGenerating = false;

/* Replies are rendered with innerHTML, and they carry facility names and free
   text that came from an uploaded file. Escaped so an uploaded value cannot
   inject markup into the chat surface. */
function escapeChatText(s) {
  return String(s).replace(/[&<>"']/g, (c) => (
    { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]
  ));
}

/**
 * Initialize Chatbot Modal & Global Listeners
 */
export function initChatbot() {
  // Bind Escape key to close modal
  document.addEventListener('keydown', (e) => {
    if (e.key === 'Escape') {
      closeChatbotModal();
    }
  });

  // Bind Enter key on input
  const input = document.getElementById('chatbot-modal-input');
  if (input) {
    input.addEventListener('keydown', (e) => {
      if (e.key === 'Enter' && !e.shiftKey) {
        e.preventDefault();
        sendChatbotInput();
      }
    });
  }

  // Bind Send button
  const sendBtn = document.getElementById('chatbot-modal-send');
  if (sendBtn) {
    sendBtn.onclick = () => sendChatbotInput();
  }
}

/**
 * Show the conversation, or the way in to one.
 *
 * Three functions each set the FAQ list and the chat view by hand — opening
 * the panel onto a restored thread, asking a question, going back. The
 * welcome card was in none of them, so "Hi there! I'm Netgravity AI, here to
 * help you explore your network" sat above every thread for its whole length,
 * introducing an assistant the reader was already talking to.
 *
 * One switch, so a fourth thing to hide cannot drift out of step with it.
 */
function showConversation(on) {
  const faqSection = document.getElementById('chatbot-faq-section');
  const chatView = document.getElementById('chatbot-chat-view');
  const welcome = document.querySelector('.chatbot-welcome-banner');
  if (faqSection) faqSection.style.display = on ? 'none' : 'block';
  if (welcome) welcome.style.display = on ? 'none' : '';
  if (chatView) chatView.style.display = on ? 'flex' : 'none';
  return chatView;
}

/**
 * Open Ask Netgravity Modal
 */
/**
 * Open Ask Netgravity.
 *
 * `prefill` puts a question in the box and leaves it there for the user to
 * send. It is not sent automatically: a question to the assistant runs a
 * solve and a reasoning pass, and starting minutes of work from one click
 * elsewhere in the app would be a surprise, not a shortcut.
 */
export function openChatbotModal({ prefill = '' } = {}) {
  const overlay = document.getElementById('chatbot-modal-overlay');
  if (!overlay) return;

  overlay.classList.add('active');
  overlay.style.display = 'flex';

  // Greet the person who is actually signed in. The markup greeted "Hi Amit!"
  // for every user of the application.
  if (typeof window.applyIdentity === 'function') window.applyIdentity();

  setTimeout(() => {
    const input = document.getElementById('chatbot-modal-input');
    if (!input) return;
    if (prefill) input.value = prefill;
    input.focus();
  }, 100);

  // Bring back the thread this tab was already having, if there is one.
  restoreConversation().then((restored) => {
    if (!restored) return;
    showConversation(true);
    renderChatMessages();
  });
}

/**
 * Close Ask Netgravity Modal
 */
export function closeChatbotModal() {
  const overlay = document.getElementById('chatbot-modal-overlay');
  if (overlay) {
    overlay.classList.remove('active');
    overlay.style.display = 'none';
  }
}

/**
 * Switch to FAQ Home View
 */
export function resetChatbotView() {
  chatMessages = [];
  // Going back to the FAQ list ends the thread. Keeping the id would make the
  // next question a follow-up to a conversation the user can no longer see.
  storeConversationId(null);
  const chatView = showConversation(false);
  if (chatView) chatView.innerHTML = '';

  const input = document.getElementById('chatbot-modal-input');
  if (input) {
    input.value = '';
    input.focus();
  }
}

import { chatService } from './integration/services/chat-service.js';
import { getActiveProjectId, onProjectChange } from './integration/project-context.js';

/**
 * The server-side conversation this tab is continuing, PER PROJECT.
 *
 * Kept in sessionStorage, not just in a module variable. The orchestrator has
 * stored every turn since Phase 3 and exposes them at
 * `/orchestrator/chat/<id>/history`, but the id lived only in memory — so a
 * page reload started a new conversation, the stored thread was orphaned, and
 * a follow-up like "Why?" had no previous turn to refer to and was answered as
 * if it were a fresh question.
 *
 * The key is scoped by project id. It was a single global key, so switching
 * projects carried the previous project's thread into the new one: asking
 * about the sample network and then opening a client project showed that
 * client an answer naming PLANT_SOUTH and DC_WEST — facilities from a network
 * they have no relationship to. That is a confidentiality failure, not a
 * cosmetic one, and it is worse than a wrong answer because the reply looks
 * authoritative and belongs to somebody else.
 *
 * sessionStorage rather than localStorage: a conversation belongs to the tab
 * the person is working in.
 */
const CONVERSATION_KEY_PREFIX = 'ng_chat_conversation_id:';

function conversationKey(projectId) {
  return `${CONVERSATION_KEY_PREFIX}${projectId || 'none'}`;
}

// Declared before the helpers that assign it: `storeConversationId` writes to
// this binding, and a `let` referenced before its declaration is evaluated is a
// TemporalDeadZone error rather than an undefined read.
let conversationId = null;
//: The project the id above belongs to. Compared before every use, so a
//: conversation can never be spoken into a project it was not started in.
let conversationProjectId = null;

function loadConversationId(projectId) {
  try {
    return sessionStorage.getItem(conversationKey(projectId)) || null;
  } catch {
    return null;          // private mode, blocked storage: fall back to memory
  }
}

function storeConversationId(id) {
  conversationId = id || null;
  conversationProjectId = getActiveProjectId();
  try {
    const key = conversationKey(conversationProjectId);
    if (id) sessionStorage.setItem(key, id);
    else sessionStorage.removeItem(key);
  } catch {
    /* memory-only for this tab */
  }
}

/**
 * Drop the visible transcript and the thread id when the project changes.
 *
 * Clearing is the safe direction: the worst case is a user re-asking a
 * question, against the worst case of one client reading another's answer.
 */
function resetConversationForProject() {
  const pid = getActiveProjectId();
  if (pid === conversationProjectId) return;
  conversationProjectId = pid;
  conversationId = loadConversationId(pid);
  chatMessages.length = 0;
  const body = document.getElementById('chatbot-messages');
  if (body) body.innerHTML = '';
  renderChatMessages();
}

conversationProjectId = getActiveProjectId();
conversationId = loadConversationId(conversationProjectId);

// Every screen already refetches on a project change; the assistant now does
// the same instead of keeping a thread that belongs to the project just left.
onProjectChange(() => resetConversationForProject());

/**
 * Reload the visible transcript from the server's own record.
 *
 * Rendered from stored turns rather than kept in the browser, so what is shown
 * after a reload is what the orchestrator actually answered — not a client-side
 * copy that could drift from it.
 */
async function restoreConversation() {
  if (!conversationId || chatMessages.length) return false;
  let turns = [];
  try {
    const res = await chatService.getHistory(conversationId);
    turns = (res && res.turns) || [];
  } catch {
    // A conversation the server no longer has is not an error worth showing:
    // start a fresh one silently.
    storeConversationId(null);
    return false;
  }
  if (!turns.length) return false;

  turns.forEach((turn) => {
    if (turn.user_input) {
      chatMessages.push({ role: 'user', text: turn.user_input });
    }
    const text = turn.reply || turn.clarification || '';
    if (text) {
      chatMessages.push({
        role: 'ai',
        topic: (turn.intent && turn.intent !== 'UNKNOWN')
          ? turn.intent : 'EARLIER IN THIS SESSION',
        text: escapeChatText(text),
      });
    }
  });
  return chatMessages.length > 0;
}

/**
 * Ask a specific predefined prompt or FAQ
 */
/**
 * Ask the assistant.
 *
 * `pickedOption` is set only when the user ANSWERED a clarification by
 * choosing one of its options. It travels alongside the message so the server
 * resumes the request the question was asked about; without it "Lowest cost"
 * is just a new message, and the server rightly makes nothing of it.
 */
export async function askChatbotPrompt(query, pickedOption = null) {
  if (!query || isGenerating) return;

  showConversation(true);

  // Add User Message
  chatMessages.push({ role: 'user', text: query });
  renderChatMessages();

  // Refuse before asking, when there is nothing to ask about.
  //
  // `/orchestrator/chat` falls back to the network the orchestrator boots with
  // if no snapshot is supplied, and answers in full: a user who had uploaded
  // nothing was told "I see a business network cost of 150,627.70 per period"
  // for a synthetic network they have never seen. The dashboard behind the
  // assistant was, correctly, showing dashes at the time.
  if (!getActiveSnapshotId()) {
    chatMessages.push({
      role: 'ai',
      topic: 'NO NETWORK LOADED',
      text: 'This project has no analysed network yet, so I have nothing to '
          + 'report on. Upload your dataset and I will answer from your own '
          + 'facilities, corridors and costs.',
    });
    renderChatMessages();
    return;
  }

  // Show typing indicator & generate response
  isGenerating = true;
  showTypingIndicator();

  try {
    // Never continue a thread that belongs to another project. The listener
    // above normally clears it, but a project switch that races an in-flight
    // send would otherwise post this question into the previous project's
    // conversation — and the orchestrator would answer it with that thread's
    // context.
    const threadId = (conversationProjectId === getActiveProjectId())
      ? conversationId : null;
    const res = await chatService.sendMessage(query, threadId, null, pickedOption);
    removeTypingIndicator();
    isGenerating = false;

    if (res && res.conversation_id) storeConversationId(res.conversation_id);

    // The endpoint's answer field is `reply`. This read `res.response`, which
    // the API has never returned — so every successful answer fell through to
    // the "did not return an answer" branch below and the assistant appeared
    // to fetch nothing at all. `response` is still accepted in case a
    // deployment predates the rename.
    const answer = res && (res.reply || res.response);
    const clarification = res && res.clarification;

    if (answer) {
      chatMessages.push({
        role: 'ai',
        // `intent` is 'UNKNOWN' when the request was not understood; labelling
        // that "ORCHESTRATOR RESPONSE" made a refusal look like an answer.
        topic: (res.intent && res.intent !== 'UNKNOWN')
          ? res.intent
          : (res.status === 'UNSUPPORTED' ? 'NOT UNDERSTOOD' : 'ORCHESTRATOR RESPONSE'),
        text: escapeChatText(answer),
        actionText: 'Explore in Digital Twin →',
        actionTab: 'twin',
      });
    } else if (clarification) {
      // `clarification` is a ClarificationRequest — {kind, question, options,
      // missing_parameter} — not a string. It was rendered with
      // escapeChatText(), which prints "[object Object]"; and even read
      // correctly, a question whose options can only be answered by retyping
      // them is a question the server then reads as a different request.
      chatMessages.push({
        role: 'ai',
        topic: 'NEEDS CLARIFICATION',
        text: escapeChatText(clarification.question || clarification),
        // Rendered as buttons. Picking one resumes the original request.
        choices: (clarification.options || []).filter((o) => o && o.id),
      });
    } else {
      // The orchestrator answered but produced no text. Say so; do not
      // synthesise a business narrative in its place.
      chatMessages.push({
        role: 'ai',
        topic: 'NO RESPONSE',
        text: 'The assistant did not return an answer for that question. '
            + 'Please rephrase it, or open the Digital Twin to inspect the '
            + 'network directly.',
        isError: true,
      });
    }
  } catch (err) {
    removeTypingIndicator();
    isGenerating = false;
    // Phase 10.0: this branch previously called generateAIResponse(query),
    // which emitted a confident fabricated briefing — "96.7% On-time SLA",
    // "Delhi NCR DC 94% utilization", "all 19 India facilities" — none of it
    // from the engine, and indistinguishable from a real answer. An assistant
    // that cannot reach its engine must say that, not invent numbers.
    const detail = (err && err.message) ? err.message : 'the assistant is unreachable';
    chatMessages.push({
      role: 'ai',
      topic: 'ASSISTANT UNAVAILABLE',
      text: `I could not reach the analysis engine, so I have no grounded answer `
          + `to give (${detail}). The dashboard KPIs and Digital Twin remain `
          + `available and are unaffected.`,
      isError: true,
    });
  }
  renderChatMessages();
}

/**
 * Send user input from the bottom search bar
 */
export function sendChatbotInput() {
  const input = document.getElementById('chatbot-modal-input');
  if (!input || !input.value.trim() || isGenerating) return;

  const query = input.value.trim();
  input.value = '';

  askChatbotPrompt(query);
}

/**
 * Generate intelligent contextual response
 */

/**
 * Render Chat Bubbles in Chat View
 */
/* No engine label above the thread.
 *
 * It read "NetGravity v2.0.0 - grounded answers, deterministic", built from
 * `/api/status`. Both halves were true and neither was the reader's: a build
 * number and whether a language model happened to be reachable are facts
 * about the deployment, not about the answer being read. What grounds an
 * answer is said by the answer — every reply carries the figures it used.
 */

function renderChatMessages() {
  const chatView = document.getElementById('chatbot-chat-view');
  if (!chatView) return;

  chatView.innerHTML = `
    <div class="chatbot-back-row">
      <button class="chatbot-back-btn" data-action="resetChatbotView">
        ← Back to FAQs & suggestions
      </button>
    </div>
    ${chatMessages.map(msg => {
      if (msg.role === 'user') {
        return `
          <div class="chat-msg-row user">
            <div class="chat-bubble-user">${escapeChatText(msg.text)}</div>
          </div>
        `;
      } else {
        return `
          <div class="chat-msg-row ai">
            <div class="chat-bubble-ai">
              ${msg.topic ? `<div class="ai-badge-chip">✦ ${msg.topic}</div>` : ''}
              <div>${msg.text}</div>
              ${(msg.choices && msg.choices.length) ? `
                <div class="chat-choices">
                  ${msg.choices.map((choice) => `
                    <button type="button" class="chat-choice-btn"
                            data-clarify-option="${escapeChatText(choice.id)}"
                            data-clarify-label="${escapeChatText(choice.label || choice.id)}"
                            ${msg.answered ? 'disabled' : ''}>
                      <span class="chat-choice-label">${escapeChatText(choice.label || choice.id)}</span>
                      ${choice.description
                        ? `<span class="chat-choice-desc">${escapeChatText(choice.description)}</span>`
                        : ''}
                    </button>`).join('')}
                </div>
              ` : ''}
              ${msg.actionText && msg.actionTab ? `
                <button class="action-link-btn" data-action="exploreInTwin" data-arg="${escapeChatText(msg.actionTab)}" data-topic="${escapeChatText(msg.topic || 'Network Optimization')}">
                  ${msg.actionText}
                </button>
              ` : ''}
            </div>
          </div>
        `;
      }
    }).join('')}
  `;

  chatView.querySelectorAll('[data-clarify-option]').forEach((btn) => {
    btn.addEventListener('click', () => {
      if (btn.disabled) return;
      // Only the bubble that was answered is spent. Earlier questions in the
      // thread stay as they were rather than appearing to have been answered.
      const bubble = btn.closest('.chat-bubble-ai');
      const msg = chatMessages.find((m) => m.choices && !m.answered);
      if (msg) msg.answered = true;
      if (bubble) {
        bubble.querySelectorAll('[data-clarify-option]')
          .forEach((b) => { b.disabled = true; });
      }
      askChatbotPrompt(btn.getAttribute('data-clarify-label'),
                       btn.getAttribute('data-clarify-option'));
    });
  });

  // Scroll to bottom
  const body = document.getElementById('chatbot-modal-body');
  if (body) {
    body.scrollTop = body.scrollHeight;
  }
}

/* ─── waiting for a reply ─────────────────────────────────────────────
   Three dots and nothing else, for however long the orchestrator took. On a
   question that reaches a solve that is twenty seconds of a bubble that never
   changes, and a reader with no way to tell a slow answer from a dead one.

   These words make no claim about the system. They are deliberately whimsical
   — nobody reads "Percolating" as a description of a capability — because the
   alternative, inventing plausible-sounding stage names, is the exact failure
   this codebase has twice had to undo. What IS true is stated: the dots keep
   moving because a request is genuinely in flight.

   There is no clock. A count of seconds was shown here past ten seconds, on
   the reasoning that a long wait needs to be told from a dead one — but the
   moving words and dots already say the request is alive, and a number
   climbing next to them turns waiting into watching it climb.

   The loading DIALOG is not used here. A modal over the conversation would
   hide the thing the reader is waiting on, and a chat turn is a message in a
   thread rather than a screen being rebuilt. */
const THINKING_WORDS = [
  'Thinking', 'Pondering', 'Percolating', 'Mulling', 'Ruminating',
  'Cogitating', 'Noodling', 'Simmering', 'Brewing', 'Distilling',
  'Untangling', 'Puzzling', 'Deliberating', 'Considering', 'Musing',
  'Sifting', 'Weighing', 'Tracing', 'Wondering', 'Marinating',
  'Germinating', 'Whirring', 'Contemplating', 'Chewing it over',
];

/** How long each word is shown. Long enough to read, short enough to notice. */
const THINKING_WORD_MS = 2400;

let thinkingTimer = null;

/** A shuffled queue, so the words do not repeat until all have been shown. */
function shuffledWords() {
  const words = THINKING_WORDS.slice();
  for (let i = words.length - 1; i > 0; i -= 1) {
    const j = Math.floor(Math.random() * (i + 1));
    [words[i], words[j]] = [words[j], words[i]];
  }
  return words;
}

/**
 * Show that a reply is being waited for.
 *
 * One timer, started here and cleared in `removeTypingIndicator`, which every
 * exit path from `sendChatMessage` calls — including the failure path.
 */
function showTypingIndicator() {
  const chatView = document.getElementById('chatbot-chat-view');
  if (!chatView) return;
  removeTypingIndicator();

  const typingEl = document.createElement('div');
  typingEl.id = 'chatbot-typing-indicator';
  typingEl.className = 'chat-msg-row ai';
  // One stable label for a screen reader. Announcing each word as it changed
  // would read the whole list aloud to someone waiting for an answer.
  typingEl.innerHTML = `
    <div class="chat-bubble-ai chat-thinking" role="status"
         aria-label="Waiting for a reply">
      <span class="chat-thinking-word" aria-hidden="true"></span>
      <span class="typing-indicator" aria-hidden="true">
        <span class="typing-dot"></span>
        <span class="typing-dot"></span>
        <span class="typing-dot"></span>
      </span>
    </div>
  `;
  chatView.appendChild(typingEl);

  const wordEl = typingEl.querySelector('.chat-thinking-word');
  let queue = shuffledWords();

  const nextWord = () => {
    if (!queue.length) queue = shuffledWords();
    const word = queue.shift();
    wordEl.textContent = `${word}…`;
    // Restarted deliberately: the fade belongs to the word, and each new word
    // gets its own.
    wordEl.classList.remove('is-in');
    void wordEl.offsetWidth;
    wordEl.classList.add('is-in');
  };

  nextWord();
  thinkingTimer = setInterval(nextWord, THINKING_WORD_MS);

  const body = document.getElementById('chatbot-modal-body');
  if (body) body.scrollTop = body.scrollHeight;
}

/** Stop waiting: the reply arrived, or it failed. */
function removeTypingIndicator() {
  if (thinkingTimer) {
    clearInterval(thinkingTimer);
    thinkingTimer = null;
  }
  const typingEl = document.getElementById('chatbot-typing-indicator');
  if (typingEl) typingEl.remove();
}

// Expose globally on window
if (typeof window !== 'undefined') {
  window.openChatbotModal = openChatbotModal;
  window.closeChatbotModal = closeChatbotModal;
  window.askChatbotPrompt = askChatbotPrompt;
  window.resetChatbotView = resetChatbotView;
  window.sendChatbotInput = sendChatbotInput;
}
