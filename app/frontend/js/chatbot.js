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

const FAQ_KNOWLEDGE_BASE = {
  "What is driving the increase in transportation cost?": {
    topic: "COST & NETWORK TELEMETRY",
    answer: `Transportation cost increased by <strong>₹3.2L (+8.4%)</strong> primarily driven by two factors:
      <ol style="margin: 8px 0 10px 18px; padding: 0; line-height: 1.65;">
        <li><strong>Expedited Freight on Baddi → Delhi:</strong> Peak volume (9,400 u/d) forced high spot-market freight rates (+22% premium).</li>
        <li><strong>Cross-Regional Inefficiency:</strong> Eastern corridor shipments bypassing regional consolidation added ₹1.4L in auxiliary lane surcharges.</li>
      </ol>
      Rebalancing 12% of Baddi volume to Kolkata DC can eliminate expedited surcharges and save <strong>₹2.4L/mo</strong>.`,
    actionText: "Simulate Rebalancing in Scenario Planner →",
    actionTab: "scenarios"
  },
  "Which DCs are operating near capacity?": {
    topic: "FACILITY CAPACITY ASSESSMENT",
    answer: `<strong>Delhi NCR DC</strong> is the critical bottleneck, operating at <strong>94.0% capacity utilization</strong> (9,400 units/day against a 10,000 u/d ceiling).
      <p style="margin: 8px 0 6px;">Demand forecast projects a <strong>+14.2% demand surge</strong> by December 2026 (108% projected utilization), which will cause severe dispatch spillover and SLA penalties.</p>
      In contrast, <strong>Kolkata DC (53.3%)</strong> and <strong>Mumbai DC (75.6%)</strong> possess abundant spare buffer.`,
    actionText: "Inspect Delhi NCR DC in Cockpit →",
    actionTab: "home"
  },
  "Show me the top cost optimization opportunities.": {
    topic: "PRESCRIPTIVE COST REDUCTION",
    answer: `NetGravity mathematical engine has identified 3 high-impact cost reduction opportunities:
      <ol style="margin: 8px 0 10px 18px; padding: 0; line-height: 1.65;">
        <li><strong>Baddi Flow Rebalancing:</strong> Saves <strong>₹2.4L/mo (-7.8%)</strong> with zero capital expenditure by shifting overflow volume to Kolkata DC.</li>
        <li><strong>Multi-Drop Western Corridor Consolidation:</strong> Saves <strong>₹1.8L/mo (-5.2%)</strong> on Mumbai–Pune routes.</li>
        <li><strong>Direct Plant Bypass for Tier-1 Markets:</strong> Saves <strong>₹95K/mo (-2.8%)</strong> by bypassing intermediary cross-docks.</li>
      </ol>`,
    actionText: "Review Recommendations →",
    actionTab: "recommendations"
  },
  "What scenarios have the highest cost savings?": {
    topic: "SCENARIO TRADE-OFF EVALUATION",
    answer: `Ranking of evaluated MILP optimization scenarios by net savings:
      <ol style="margin: 8px 0 10px 18px; padding: 0; line-height: 1.65;">
        <li><strong>Scenario 1 (Rebalance Baddi Volume):</strong> <strong>₹2.4L net monthly savings (-7.8%)</strong> · SLA: 96.7% · CapEx: ₹0.</li>
        <li><strong>Scenario 3 (Hub Consolidation):</strong> <strong>₹1.9L net monthly savings (-6.1%)</strong> · SLA: 95.8%.</li>
        <li><strong>Scenario 2 (Delhi NCR Brownfield Expansion):</strong> <strong>₹1.2L net return</strong> after factoring in ₹15L expansion CapEx amortized.</li>
      </ol>`,
    actionText: "Open Scenario Planning Workspace →",
    actionTab: "scenarios"
  },
  "How is service level expected to change in Q4?": {
    topic: "SLA & DEMAND PROJECTIONS",
    answer: `Current baseline network SLA is healthy at <strong>96.7%</strong> (Target: ≥95.0%).
      <p style="margin: 8px 0 6px;">However, in Q4 (Oct–Dec 2026), North India festive demand surge (+14.2%) without flow reallocation will breach Delhi DC throughput limits, causing regional dispatch SLA to drop sharply to <strong>91.2%</strong>.</p>
      Implementing the AI rebalancing strategy preserves SLA at <strong>96.7%</strong> throughout Q4.`,
    actionText: "View Demand Forecast Projections →",
    actionTab: "forecast"
  },
  "Which lanes are most at risk of disruption?": {
    topic: "SUPPLY CHAIN RESILIENCE",
    answer: `Two critical network corridors exhibit high disruption exposure:
      <ul style="margin: 8px 0 10px 18px; padding: 0; line-height: 1.65;">
        <li><strong>Baddi → Delhi NCR:</strong> Heavy single-carrier concentration + winter fog corridor delay risk (historical 3.2-day transit variance).</li>
        <li><strong>Kolkata → Guwahati:</strong> Mountain corridor infrastructure vulnerability with 8.4% shipment delay frequency.</li>
      </ul>
      Contracting backup secondary 3PL carriers is recommended to insulate against SLA penalties.`,
    actionText: "View Digital Twin Topology →",
    actionTab: "twin"
  }
};

let chatMessages = [];
let isGenerating = false;

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

  setTimeout(() => {
    const input = document.getElementById('chatbot-modal-input');
    if (input) input.focus();
  }, 100);
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

/**
 * Ask a specific predefined prompt or FAQ
 */
export function askChatbotPrompt(query) {
  if (!query || isGenerating) return;

  const faqSection = document.getElementById('chatbot-faq-section');
  const chatView = document.getElementById('chatbot-chat-view');

  if (faqSection) faqSection.style.display = 'none';
  if (chatView) chatView.style.display = 'flex';

  // Add User Message
  chatMessages.push({ role: 'user', text: query });
  renderChatMessages();

  // Show typing indicator & generate response
  isGenerating = true;
  showTypingIndicator();

  setTimeout(() => {
    removeTypingIndicator();
    isGenerating = false;

    const response = generateAIResponse(query);
    chatMessages.push({
      role: 'ai',
      topic: response.topic,
      text: response.answer,
      actionText: response.actionText,
      actionTab: response.actionTab
    });
    renderChatMessages();
  }, 450);
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
function generateAIResponse(query) {
  // Check exact KB match first
  if (FAQ_KNOWLEDGE_BASE[query]) {
    return FAQ_KNOWLEDGE_BASE[query];
  }

  const q = query.toLowerCase();

  if (q.includes('delhi') || q.includes('capacity') || q.includes('utilis') || q.includes('bottleneck')) {
    return {
      topic: "FACILITY TELEMETRY & CAPACITY",
      answer: `<strong>Delhi NCR DC</strong> is operating at <strong>94.0% utilization</strong> with 600 u/d headroom remaining. Demand forecast indicates a <strong>+14.2% growth in Q4</strong> which will exceed total capacity. NetGravity recommends immediate flow diversion to <strong>Kolkata DC</strong>.`,
      actionText: "Explore Scenario Planning →",
      actionTab: "scenarios"
    };
  }

  if (q.includes('cost') || q.includes('saving') || q.includes('expense') || q.includes('reduce') || q.includes('opportunit')) {
    return {
      topic: "PRESCRIPTIVE COST OPTIMIZATION",
      answer: `NetGravity has identified <strong>₹2.4L/month in actionable cost savings</strong> by reallocating Baddi manufacturing plant volume between Delhi and Kolkata corridors, reducing spot-market transportation penalties while maintaining SLA at <strong>96.7%</strong>.`,
      actionText: "View Recommendation Details →",
      actionTab: "recommendations"
    };
  }

  if (q.includes('scenario') || q.includes('rebalance') || q.includes('expand') || q.includes('plan')) {
    return {
      topic: "OPTIMIZATION SCENARIO SOLVER",
      answer: `The mathematical solver evaluated 4 distinct network configurations. <strong>Scenario 1 (Rebalance Baddi Volume)</strong> achieved optimal multi-echelon cost efficiency with <strong>-7.8% total network cost</strong> and zero required CapEx.`,
      actionText: "Open Scenario Planning →",
      actionTab: "scenarios"
    };
  }

  if (q.includes('forecast') || q.includes('demand') || q.includes('future') || q.includes('predict')) {
    return {
      topic: "PREDICTIVE DEMAND TELEMETRY",
      answer: `Demand across North India is forecasted to expand by <strong>14.2% over the next 3 months</strong>. External economic indicators and festive consumption signals suggest sustained volume pressure across North and Western hubs through December 2026.`,
      actionText: "View Demand Forecast →",
      actionTab: "forecast"
    };
  }

  if (q.includes('insight') || q.includes('summar') || q.includes('key')) {
    return {
      topic: "EXECUTIVE NETWORK SUMMARY",
      answer: `Network executive summary:
        <ul style="margin: 6px 0 8px 18px; padding: 0; line-height: 1.6;">
          <li><strong>Cost:</strong> ₹11.8L current period (-3.2% vs previous).</li>
          <li><strong>Service Level:</strong> 96.7% On-time SLA (Target: 95.0%).</li>
          <li><strong>Key Bottleneck:</strong> Delhi NCR DC (94% utilization, high Q4 breach risk).</li>
          <li><strong>Key Opportunity:</strong> Kolkata DC spare capacity (53.3% utilization).</li>
        </ul>`,
      actionText: "View Full Insights Page →",
      actionTab: "insights"
    };
  }

  // Fallback intelligent answer
  return {
    topic: "NETGRAVITY AI ADVISORY",
    answer: `Regarding <em>"${query}"</em>: NetGravity's network optimization engine continuously evaluates real-time throughput, demand forecasts, and transportation economics across all 19 India facilities. You can simulate specific parameter adjustments in the <strong>Scenario Planning</strong> workspace.`,
    actionText: "Open Scenario Planning →",
    actionTab: "scenarios"
  };
}

/**
 * Render Chat Bubbles in Chat View
 */
function renderChatMessages() {
  const chatView = document.getElementById('chatbot-chat-view');
  if (!chatView) return;

  chatView.innerHTML = `
    <div class="chatbot-back-row">
      <button class="chatbot-back-btn" onclick="window.resetChatbotView && window.resetChatbotView()">
        ← Back to FAQs & suggestions
      </button>
      <span style="font-size: 11.5px; color: #9ca3af; font-weight: 500;">NetGravity AI v2.4</span>
    </div>
    ${chatMessages.map(msg => {
      if (msg.role === 'user') {
        return `
          <div class="chat-msg-row user">
            <div class="chat-bubble-user">${msg.text}</div>
          </div>
        `;
      } else {
        return `
          <div class="chat-msg-row ai">
            <div class="chat-bubble-ai">
              ${msg.topic ? `<div class="ai-badge-chip">✦ ${msg.topic}</div>` : ''}
              <div>${msg.text}</div>
              ${msg.actionText && msg.actionTab ? `
                <button class="action-link-btn" onclick="window.navigateToTab && window.navigateToTab('${msg.actionTab}'); window.closeChatbotModal && window.closeChatbotModal();">
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
