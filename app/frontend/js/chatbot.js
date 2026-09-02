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
 * Open Ask Netgravity Modal
 */
export function openChatbotModal() {
  const overlay = document.getElementById('chatbot-modal-overlay');
  if (!overlay) return;

  overlay.classList.add('active');
  overlay.style.display = 'flex';

  // Greet the person who is actually signed in. The markup greeted "Hi Amit!"
  // for every user of the application.
  if (typeof window.applyIdentity === 'function') window.applyIdentity();

  setTimeout(() => {
    const input = document.getElementById('chatbot-modal-input');
    if (input) input.focus();
  }, 100);

  // Bring back the thread this tab was already having, if there is one.
  restoreConversation().then((restored) => {
    if (!restored) return;
    const faqSection = document.getElementById('chatbot-faq-section');
    const chatView = document.getElementById('chatbot-chat-view');
    if (faqSection) faqSection.style.display = 'none';
    if (chatView) chatView.style.display = 'flex';
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
  const faqSection = document.getElementById('chatbot-faq-section');
  const chatView = document.getElementById('chatbot-chat-view');

  if (faqSection) faqSection.style.display = 'block';
  if (chatView) {
    chatView.style.display = 'none';
    chatView.innerHTML = '';
  }

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
export async function askChatbotPrompt(query) {
  if (!query || isGenerating) return;

  const faqSection = document.getElementById('chatbot-faq-section');
  const chatView = document.getElementById('chatbot-chat-view');

  if (faqSection) faqSection.style.display = 'none';
  if (chatView) chatView.style.display = 'flex';

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
    const res = await chatService.sendMessage(query, threadId);
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
      chatMessages.push({
        role: 'ai',
        topic: 'NEEDS CLARIFICATION',
        text: escapeChatText(clarification),
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
/**
 * What is actually answering, from the server's own status.
 *
 * The chat header read "NetGravity AI v2.4" — a version number that appears
 * nowhere in this codebase (the application reports 2.0.0) and told the user
 * nothing about whether a language model was even reachable.
 */
function engineLabel() {
  const s = (typeof window !== 'undefined') ? window.__ngServerStatus : null;
  if (!s) return 'NetGravity analysis engine';
  const version = s.version ? ` v${s.version}` : '';
  const llm = s.orchestrator && s.orchestrator.llm_available;
  return `NetGravity${version} - ${llm ? 'grounded answers, model assisted' : 'grounded answers, deterministic'}`;
}

function renderChatMessages() {
  const chatView = document.getElementById('chatbot-chat-view');
  if (!chatView) return;

  chatView.innerHTML = `
    <div class="chatbot-back-row">
      <button class="chatbot-back-btn" data-action="resetChatbotView">
        ← Back to FAQs & suggestions
      </button>
      <span style="font-size: 11.5px; color: #9ca3af; font-weight: 500;">${escapeChatText(engineLabel())}</span>
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

  // Scroll to bottom
  const body = document.getElementById('chatbot-modal-body');
  if (body) {
    body.scrollTop = body.scrollHeight;
  }
}

/**
 * Show animated typing dots indicator
 */
function showTypingIndicator() {
  const chatView = document.getElementById('chatbot-chat-view');
  if (!chatView) return;

  const typingEl = document.createElement('div');
  typingEl.id = 'chatbot-typing-indicator';
  typingEl.className = 'chat-msg-row ai';
  typingEl.innerHTML = `
    <div class="chat-bubble-ai" style="padding: 10px 16px;">
      <div class="typing-indicator">
        <span class="typing-dot"></span>
        <span class="typing-dot"></span>
        <span class="typing-dot"></span>
      </div>
    </div>
  `;
  chatView.appendChild(typingEl);

  const body = document.getElementById('chatbot-modal-body');
  if (body) body.scrollTop = body.scrollHeight;
}

/**
 * Remove animated typing indicator
 */
function removeTypingIndicator() {
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
