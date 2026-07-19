const invoke = (...args) => window.__TAURI__.core.invoke(...args);
const openDialog = (options = {}) => invoke('plugin:dialog|open', { options });

const API = {
    getClaudeNodes: () => invoke('get_claude_nodes'),
    addClaudeNode: (data) => invoke('add_claude_node', { request: data }),
    removeClaudeNode: (name) => invoke('remove_claude_node', { name }),
    setCurrentClaudeNode: (name) => invoke('set_current_claude_node', { name }),
    startClaude: (name, data) => invoke('start_claude', { name, request: data }),
    getCodexProfiles: () => invoke('get_codex_profiles'),
    addCodexProfile: (data) => invoke('add_codex_profile', { request: data }),
    removeCodexProfile: (name) => invoke('remove_codex_profile', { name }),
    startCodex: (name, data) => invoke('start_codex', { name, request: data }),
    // Usage Stats
    checkStatsAvailable: () => invoke('check_stats_available'),
    getNodeUsageStats: (nodeName) => invoke('get_node_usage_stats', { nodeName }),
    getAllNodesUsage: (limit) => invoke('get_all_nodes_usage', { limit }),
    getUsageOverview: () => invoke('get_usage_overview'),
};

let currentView = 'claude';
let claudeData = { nodes: {}, current: null };
let codexData = { profiles: [] };
let selectedNode = null;
let launchContext = { type: null, name: null, folder: null };

// Settings State
let settings = {
    defaultMode: 'new',
    defaultPerm: 'default',
    rememberFolder: false,
    autoRefresh: true,
    confirmDelete: true,
    sortBy: 'name',
    showFullToken: false,
    theme: 'light',
    lastFolder: null,
};

function loadSettings() {
    const saved = localStorage.getItem('app_settings');
    if (saved) {
        try {
            settings = { ...settings, ...JSON.parse(saved) };
        } catch (e) {
            console.error('Failed to load settings:', e);
        }
    }
    applySettings();
}

function saveSettings() {
    localStorage.setItem('app_settings', JSON.stringify(settings));
}

function applySettings() {
    // Apply to UI
    document.getElementById('settingDefaultMode').value = settings.defaultMode;
    document.getElementById('settingDefaultPerm').value = settings.defaultPerm;
    document.getElementById('settingRememberFolder').checked = settings.rememberFolder;
    document.getElementById('settingAutoRefresh').checked = settings.autoRefresh;
    document.getElementById('settingConfirmDelete').checked = settings.confirmDelete;
    document.getElementById('settingSortBy').value = settings.sortBy;
    document.getElementById('settingShowFullToken').checked = settings.showFullToken;
    document.getElementById('settingTheme').value = settings.theme;

    // Apply theme
    applyTheme(settings.theme);
}

function applyTheme(theme) {
    // Theme support can be expanded later
    if (theme === 'dark') {
        document.body.classList.add('dark-theme');
    } else {
        document.body.classList.remove('dark-theme');
    }
}

function guessProvider(url) {
    if (!url) return { name: 'API', cls: 'other' };
    const u = url.toLowerCase();
    if (u.includes('anthropic') || u.includes('claude')) return { name: 'Anthropic', cls: 'anthropic' };
    if (u.includes('openai')) return { name: 'OpenAI', cls: 'openai' };
    const m = u.match(/\/\/(?:www\.)?([^./]+)/);
    return { name: m ? m[1].charAt(0).toUpperCase() + m[1].slice(1) : 'API', cls: 'other' };
}

function shortUrl(url) {
    if (!url) return '-';
    try {
        const u = new URL(url);
        return u.host + (u.pathname !== '/' ? u.pathname : '');
    } catch {
        return url;
    }
}

function timeAgo(iso) {
    if (!iso) return '-';
    const d = new Date(iso);
    const diff = Date.now() - d.getTime();
    const mins = Math.floor(diff / 60000);
    if (mins < 1) return '刚刚';
    if (mins < 60) return `${mins} 分钟前`;
    const hrs = Math.floor(mins / 60);
    if (hrs < 24) return `${hrs} 小时前`;
    return d.toLocaleDateString('zh-CN');
}

function escapeHtml(value) {
    const div = document.createElement('div');
    div.textContent = value == null ? '' : String(value);
    return div.innerHTML;
}

function inlineValue(value) {
    return encodeURIComponent(value).replace(/'/g, '%27').replace(/\(/g, '%28').replace(/\)/g, '%29');
}

function normalizeFolder(value) {
    if (Array.isArray(value)) return value[0] || null;
    return value || null;
}

async function pickFolder() {
    const selected = await openDialog({
        directory: true,
        multiple: false,
        title: '选择项目文件夹',
        defaultPath: settings.rememberFolder ? settings.lastFolder : undefined,
    });
    const folder = normalizeFolder(selected);
    if (folder && settings.rememberFolder) {
        settings.lastFolder = folder;
        saveSettings();
    }
    return folder;
}

function setText(id, value) {
    const el = document.getElementById(id);
    if (el) el.textContent = value;
}

function setLaunchModelSuggestions(type) {
    const modelInput = document.getElementById('launchModel');
    const modelList = document.getElementById('launchModelList');
    const options = type === 'claude'
        ? [
            ['claude-opus-4-8', 'Claude Opus 4.8'],
            ['claude-opus-4-7', 'Claude Opus 4.7'],
            ['claude-opus-4-6', 'Claude Opus 4.6'],
            ['claude-sonnet-4-6', 'Claude Sonnet 4.6'],
            ['claude-haiku-4-5-20251001', 'Claude Haiku 4.5'],
            ['claude-fable-5', 'Claude Fable 5'],
        ]
        : [
            ['gpt-5.5', 'GPT 5.5'],
            ['gpt-5.1-codex', 'GPT 5.1 Codex'],
            ['gpt-5-codex', 'GPT 5 Codex'],
            ['gpt-5.1', 'GPT 5.1'],
            ['o3', 'o3'],
        ];

    modelInput.value = '';
    modelInput.placeholder = type === 'claude'
        ? '留空使用节点默认模型，可输入中转站模型 ID'
        : '留空使用 Codex 配置默认模型，可输入模型 ID';
    modelList.innerHTML = options
        .map(([value, label]) => `<option value="${escapeHtml(value)}" label="${escapeHtml(label)}"></option>`)
        .join('');
}

function configureLaunchModal(type) {
    const isClaudeMode = type === 'claude';

    document.getElementById('launchModeField').style.display = 'block';
    document.getElementById('launchPermField').style.display = 'block';
    document.getElementById('launchModelField').style.display = 'block';

    setText('launchModeLabel', '启动模式');
    setText('launchModeNewTitle', '新对话');
    setText('launchModeNewDesc', isClaudeMode ? '从零开始新的 Claude 会话' : '从零开始新的 Codex 会话');
    setText('launchModeResumeTitle', isClaudeMode ? 'Resume 上次会话' : 'Resume 会话');
    setText('launchModeResumeDesc', isClaudeMode ? '继续上一次的 Claude 对话' : '打开 Codex 会话选择器继续历史会话');

    setText('launchPermLabel', '权限模式');
    setText('launchPermDefaultTitle', '默认权限');
    setText('launchPermDefaultDesc', isClaudeMode ? '根据 Claude 提示选择允许或拒绝操作' : '使用 Codex 配置中的默认审批和沙盒策略');
    setText('launchPermBypassTitle', isClaudeMode ? '完全访问（bypassPermissions）' : '完全访问（危险绕过）');
    setText('launchPermBypassDesc', isClaudeMode ? '自动允许所有操作，无需确认' : '使用 --dangerously-bypass-approvals-and-sandbox 启动');
    setText('launchPermSandboxTitle', '沙盒模式');
    setText('launchPermSandboxDesc', isClaudeMode ? '限制文件系统访问，安全优先' : '使用只读沙盒并按需请求审批');

    setText('launchModelLabel', isClaudeMode ? '模型选择' : '模型覆盖');
    setLaunchModelSuggestions(type);
}

function updateClock() {
    const now = new Date();
    const el = document.getElementById('statusTime');
    if (!el) return;
    el.textContent = `本地时间: ${now.getFullYear()}-${String(now.getMonth() + 1).padStart(2, '0')}-${String(now.getDate()).padStart(2, '0')} ${String(now.getHours()).padStart(2, '0')}:${String(now.getMinutes()).padStart(2, '0')}:${String(now.getSeconds()).padStart(2, '0')}`;
}

setInterval(updateClock, 1000);
updateClock();

function renderClaudeView(filter = '') {
    const nodes = claudeData.nodes || {};
    const current = claudeData.current;
    const entries = Object.entries(nodes).filter(([name]) => !filter || name.toLowerCase().includes(filter.toLowerCase()));
    const count = entries.length;

    document.getElementById('claudeCurrentName').textContent = current || '-';
    document.getElementById('claudeCurrentStatus').textContent = current ? 'API 节点正在生效' : '未选择';
    document.getElementById('claudeNodeCount').textContent = count;
    document.getElementById('claudeAvailCount').textContent = count;
    document.getElementById('claudeFooterCount').textContent = `共 ${count} 个节点`;

    const tbody = document.getElementById('claudeTableBody');
    if (!count) {
        tbody.innerHTML = '<tr><td colspan="6" style="text-align:center;padding:40px;color:var(--text-3)">暂无 Claude 节点，点击「添加节点」创建</td></tr>';
        return;
    }

    tbody.innerHTML = entries.map(([name, node]) => {
        const prov = guessProvider(node.base_url);
        const isCurrent = name === current;
        const encodedName = inlineValue(name);
        const displayToken = settings.showFullToken ? (node.token || '***') : '***';
        return `
        <tr class="${isCurrent ? 'current-row' : ''} ${selectedNode?.name === name ? 'selected' : ''}" data-name="${escapeHtml(name)}" data-type="claude" onclick="selectRow('claude', decodeURIComponent('${encodedName}'))">
            <td><div class="node-name-cell"><span class="node-dot ${isCurrent ? 'active' : ''}"></span>${escapeHtml(name)}</div></td>
            <td><span class="provider-tag ${prov.cls}">${escapeHtml(prov.name)}</span></td>
            <td class="url-cell" title="${escapeHtml(node.base_url || '')}">${escapeHtml(shortUrl(node.base_url))}</td>
            <td class="token-cell">${escapeHtml(displayToken)}</td>
            <td>${isCurrent ? '<span class="status-tag current">当前</span>' : '<span class="status-tag available">可用</span>'}</td>
            <td>
                <div class="row-actions">
                    <button class="row-btn primary" onclick="event.stopPropagation();launchNode('claude', decodeURIComponent('${encodedName}'))" style="background:var(--green);border-color:var(--green)">启动</button>
                    ${isCurrent ? '' : `<button class="row-btn" onclick="event.stopPropagation();switchNode('claude', decodeURIComponent('${encodedName}'))">切换</button>`}
                    <button class="row-btn danger" onclick="event.stopPropagation();deleteNode('claude', decodeURIComponent('${encodedName}'))">删除</button>
                </div>
            </td>
        </tr>`;
    }).join('');
}

function renderCodexView(filter = '') {
    const profiles = codexData.profiles || [];
    const filtered = profiles.filter((p) => !filter || (p.name || '').toLowerCase().includes(filter.toLowerCase()));
    const count = filtered.length;

    document.getElementById('codexProfileCount').textContent = count;
    document.getElementById('codexAvailCount').textContent = count;
    document.getElementById('codexFooterCount').textContent = `共 ${count} 个配置`;

    const sorted = [...filtered].sort((a, b) => (b.lastUsedAt || '').localeCompare(a.lastUsedAt || ''));
    if (sorted.length && sorted[0].lastUsedAt) {
        document.getElementById('codexCurrentName').textContent = sorted[0].name || sorted[0].id;
        document.getElementById('codexLastUsed').textContent = timeAgo(sorted[0].lastUsedAt);
        document.getElementById('codexLastUsedSub').textContent = new Date(sorted[0].lastUsedAt).toLocaleString('zh-CN');
    } else {
        document.getElementById('codexCurrentName').textContent = '-';
        document.getElementById('codexLastUsed').textContent = '-';
        document.getElementById('codexLastUsedSub').textContent = '无记录';
    }

    const tbody = document.getElementById('codexTableBody');
    if (!count) {
        tbody.innerHTML = '<tr><td colspan="5" style="text-align:center;padding:40px;color:var(--text-3)">暂无 Codex 配置，点击「添加配置」创建</td></tr>';
        return;
    }

    tbody.innerHTML = filtered.map((profile) => {
        const name = profile.name || profile.id;
        const encodedName = inlineValue(name);
        return `
        <tr class="${selectedNode?.name === name ? 'selected' : ''}" data-name="${escapeHtml(name)}" data-type="codex" onclick="selectRow('codex', decodeURIComponent('${encodedName}'))">
            <td><div class="node-name-cell"><span class="node-dot"></span>${escapeHtml(name)}</div></td>
            <td class="url-cell" title="${escapeHtml(profile.baseUrl || '')}">${escapeHtml(shortUrl(profile.baseUrl))}</td>
            <td>${timeAgo(profile.lastUsedAt)}</td>
            <td><span class="status-tag available">可用</span></td>
            <td>
                <div class="row-actions">
                    <button class="row-btn primary" onclick="event.stopPropagation();launchNode('codex', decodeURIComponent('${encodedName}'))" style="background:var(--green);border-color:var(--green)">启动</button>
                    <button class="row-btn danger" onclick="event.stopPropagation();deleteNode('codex', decodeURIComponent('${encodedName}'))">删除</button>
                </div>
            </td>
        </tr>`;
    }).join('');
}

function render(filter = '') {
    renderClaudeView(filter);
    renderCodexView(filter);

    const claudeCount = Object.keys(claudeData.nodes || {}).length;
    const codexCount = (codexData.profiles || []).length;
    const total = claudeCount + codexCount;
    document.getElementById('totalNodesCount').textContent = total;
    document.getElementById('activeBar').style.width = total ? '100%' : '0%';
    document.getElementById('activeNodesLabel').textContent = `活跃: ${total}`;
}

function selectRow(type, name) {
    selectedNode = { type, name };
    render(document.getElementById('searchInput').value || '');
    updatePanel();
}

function updatePanel() {
    if (!selectedNode) return;
    const { type, name } = selectedNode;

    document.getElementById('panelName').textContent = name;

    if (type === 'claude') {
        const node = (claudeData.nodes || {})[name];
        if (!node) return;
        const isCurrent = name === claudeData.current;
        document.getElementById('panelSub').textContent = 'Claude API 节点 · ID: ' + name;
        document.getElementById('panelType').textContent = 'Claude Code';
        document.getElementById('panelUrl').textContent = shortUrl(node.base_url);
        document.getElementById('panelToken').textContent = node.token || '***';
        document.getElementById('panelEndpoint').textContent = node.base_url || '-';
        document.getElementById('panelBadge').style.display = isCurrent ? '' : 'none';
        document.getElementById('panelSwitchBtn').textContent = isCurrent ? '当前节点' : '切换到此节点';
        document.getElementById('panelSwitchBtn').disabled = isCurrent;
    } else {
        const profile = (codexData.profiles || []).find((p) => (p.name || p.id) === name);
        if (!profile) return;
        document.getElementById('panelSub').textContent = 'Codex API 配置 · ID: ' + (profile.id || name);
        document.getElementById('panelType').textContent = 'Codex CLI';
        document.getElementById('panelUrl').textContent = shortUrl(profile.baseUrl);
        document.getElementById('panelToken').textContent = '***';
        document.getElementById('panelEndpoint').textContent = profile.baseUrl || '-';
        document.getElementById('panelBadge').style.display = 'none';
        document.getElementById('panelSwitchBtn').textContent = '启动 Codex';
        document.getElementById('panelSwitchBtn').disabled = false;
    }
}

async function loadData() {
    try {
        const [claude, codex] = await Promise.all([
            API.getClaudeNodes(),
            API.getCodexProfiles(),
        ]);
        claudeData = claude;
        codexData = codex;
        render(document.getElementById('searchInput').value || '');
        if (selectedNode) updatePanel();
        setStatus('数据加载成功');
    } catch (err) {
        setStatus('加载失败: ' + err);
        toast('加载失败: ' + err, 'error');
    }
}

async function switchNode(type, name) {
    if (type === 'codex') {
        await launchNode('codex', name);
        return;
    }

    try {
        await API.setCurrentClaudeNode(name);
        toast(`已切换到 ${name}`);
        await loadData();
    } catch (err) {
        toast(String(err), 'error');
    }
}

async function deleteNode(type, name) {
    const needsConfirm = settings.confirmDelete;
    if (needsConfirm && !confirm(`确认删除「${name}」？`)) return;

    try {
        if (type === 'claude') {
            await API.removeClaudeNode(name);
        } else {
            await API.removeCodexProfile(name);
        }
        toast(`已删除「${name}」`);
        if (selectedNode?.name === name) selectedNode = null;
        await loadData();
    } catch (err) {
        toast(String(err), 'error');
    }
}

async function launchNode(type, name) {
    setStatus('正在选择文件夹...');
    try {
        const folder = await pickFolder();
        if (!folder) {
            setStatus('已取消');
            return;
        }

        launchContext = { type, name, folder };
        document.getElementById('launchFolder').value = folder;
        document.getElementById('launchModalTitle').textContent = type === 'claude' ? '启动 Claude 配置' : '启动 Codex 配置';

        configureLaunchModal(type);

        // Apply default settings
        const modeRadio = document.querySelector(`input[name="launchMode"][value="${settings.defaultMode}"]`);
        const permRadio = document.querySelector(`input[name="launchPerm"][value="${settings.defaultPerm}"]`);
        if (modeRadio) modeRadio.checked = true;
        if (permRadio) permRadio.checked = true;

        document.getElementById('launchModal').classList.add('open');
        setStatus('等待启动配置...');
    } catch (err) {
        toast('选择文件夹失败: ' + err, 'error');
        setStatus('启动失败');
    }
}

async function reSelectFolder() {
    try {
        const folder = await pickFolder();
        if (folder) {
            launchContext.folder = folder;
            document.getElementById('launchFolder').value = folder;
        }
    } catch (err) {
        toast(String(err), 'error');
    }
}

async function confirmLaunch() {
    const { type, name, folder } = launchContext;
    if (!folder) {
        toast('请先选择文件夹', 'error');
        return;
    }

    const mode = document.querySelector('input[name="launchMode"]:checked')?.value || 'new';
    const permission = document.querySelector('input[name="launchPerm"]:checked')?.value || 'default';
    const modelValue = document.getElementById('launchModel')?.value || '';
    const model = modelValue.trim() || null;

    setStatus('正在启动终端...');
    closeLaunchModal();

    try {
        if (type === 'claude') {
            await API.startClaude(name, { folder, mode, permission, model });
            toast('已启动 Claude');
        } else {
            await API.startCodex(name, { folder, mode, permission, model });
            toast('已启动 Codex');
        }
        setStatus('启动成功');
        await loadData();
    } catch (err) {
        toast('启动失败: ' + err, 'error');
        setStatus('启动失败');
    }
}

function closeLaunchModal() {
    document.getElementById('launchModal').classList.remove('open');
    launchContext = { type: null, name: null, folder: null };
}

function panelSwitch() {
    if (!selectedNode) return;
    switchNode(selectedNode.type, selectedNode.name);
}

function panelDelete() {
    if (!selectedNode) return;
    deleteNode(selectedNode.type, selectedNode.name);
}

function copyEndpoint() {
    const text = document.getElementById('panelEndpoint').textContent;
    navigator.clipboard.writeText(text).then(() => toast('已复制'));
}

function openModal(type) {
    document.getElementById('modalTitle').textContent = type === 'claude' ? '添加 Claude 节点' : '添加 Codex 配置';
    document.getElementById('fTokenLabel').textContent = type === 'claude' ? 'API Token' : 'API Key';
    document.getElementById('fModelField').style.display = type === 'codex' ? 'block' : 'none';
    document.getElementById('addForm').reset();
    document.getElementById('addForm').dataset.type = type;
    document.getElementById('modal').classList.add('open');
}

function closeModal() {
    document.getElementById('modal').classList.remove('open');
}

async function submitForm(event) {
    event.preventDefault();
    const type = document.getElementById('addForm').dataset.type;
    const name = document.getElementById('fName').value.trim();
    const baseUrl = document.getElementById('fBaseUrl').value.trim();
    const token = document.getElementById('fToken').value.trim();
    const model = document.getElementById('fModel').value.trim();

    try {
        if (type === 'claude') {
            await API.addClaudeNode({ name, api_key: token, base_url: baseUrl });
        } else {
            await API.addCodexProfile({
                name,
                api_key: token,
                base_url: baseUrl || null,
                model: model || null,
            });
        }
        toast(`已添加「${name}」`);
        closeModal();
        await loadData();
    } catch (err) {
        toast(String(err), 'error');
    }
}

function toast(message, type = 'success') {
    const old = document.querySelector('.toast-msg');
    if (old) old.remove();

    const el = document.createElement('div');
    el.className = 'toast-msg';
    el.style.cssText = `position:fixed;bottom:60px;right:24px;padding:10px 20px;background:${type === 'error' ? 'var(--red)' : 'var(--green)'};color:#fff;border-radius:8px;font-size:13px;font-weight:500;z-index:2000;box-shadow:var(--shadow);animation:slideUp 0.25s ease-out`;
    el.textContent = message;
    document.body.appendChild(el);
    setTimeout(() => el.remove(), 2500);
}

function setStatus(text) {
    const el = document.getElementById('statusText');
    if (el) el.textContent = text;
}

document.querySelectorAll('.nav-item').forEach((item) => {
    item.addEventListener('click', () => {
        const view = item.dataset.view;
        if (!view) return;
        currentView = view;
        document.querySelectorAll('.nav-item').forEach((i) => i.classList.remove('active'));
        item.classList.add('active');
        document.querySelectorAll('.view').forEach((v) => v.classList.remove('active'));
        document.getElementById(view + '-view').classList.add('active');
        if (view !== 'settings') {
            render(document.getElementById('searchInput').value || '');
        } else {
            // 切换到设置页时加载统计数据
            loadUsageStats();
        }
    });
});

document.getElementById('searchInput').addEventListener('input', (event) => render(event.target.value));
document.getElementById('addClaudeBtn').addEventListener('click', () => openModal('claude'));
document.getElementById('addCodexBtn').addEventListener('click', () => openModal('codex'));
document.getElementById('quickSwitchBtn').addEventListener('click', () => {
    if (currentView === 'claude') openModal('claude');
    else openModal('codex');
});
document.getElementById('modal').addEventListener('click', (event) => {
    if (event.target.id === 'modal') closeModal();
});
document.getElementById('launchModal').addEventListener('click', (event) => {
    if (event.target.id === 'launchModal') closeLaunchModal();
});

Object.assign(window, {
    loadData,
    selectRow,
    switchNode,
    deleteNode,
    launchNode,
    reSelectFolder,
    confirmLaunch,
    closeLaunchModal,
    panelSwitch,
    panelDelete,
    copyEndpoint,
    openModal,
    closeModal,
    submitForm,
    openConfigFolder,
    exportSettings,
    clearUsageData,
    resetSettings,
    openExternal,
    refreshUsageStats,
});

// Usage Stats Functions
async function loadUsageStats() {
    try {
        const available = await API.checkStatsAvailable();

        if (!available) {
            document.getElementById('statsUnavailable').style.display = 'block';
            document.getElementById('statsAvailable').style.display = 'none';
            return;
        }

        document.getElementById('statsUnavailable').style.display = 'none';
        document.getElementById('statsAvailable').style.display = 'block';

        const overview = await API.getUsageOverview();

        // 更新概览卡片
        document.getElementById('statsTodayTokens').textContent = formatTokens(overview.today_total_tokens);
        document.getElementById('statsTodayRequests').textContent = `${overview.today_request_count} 次请求`;
        document.getElementById('statsWeekTokens').textContent = formatTokens(overview.week_total_tokens);
        document.getElementById('statsWeekRequests').textContent = `${overview.week_request_count} 次请求`;

        // 更新 Top 节点排行
        renderTopNodes(overview.top_nodes);
    } catch (err) {
        console.error('加载统计数据失败:', err);
        document.getElementById('statsUnavailable').style.display = 'block';
        document.getElementById('statsAvailable').style.display = 'none';
    }
}

function formatTokens(tokens) {
    if (tokens >= 1000000) {
        return (tokens / 1000000).toFixed(1) + 'M';
    } else if (tokens >= 1000) {
        return (tokens / 1000).toFixed(1) + 'K';
    }
    return tokens.toString();
}

function renderTopNodes(nodes) {
    const container = document.getElementById('statsTopNodes');

    if (!nodes || nodes.length === 0) {
        container.innerHTML = '<p style="text-align:center;color:var(--text-3);padding:20px;">暂无数据</p>';
        return;
    }

    container.innerHTML = nodes.map((node, index) => {
        const rank = index + 1;
        const rankClass = rank === 1 ? 'rank-1' : rank === 2 ? 'rank-2' : rank === 3 ? 'rank-3' : 'rank-other';

        return `
            <div class="stats-ranking-item">
                <div class="stats-rank ${rankClass}">${rank}</div>
                <div class="stats-node-info">
                    <div class="stats-node-name">${escapeHtml(node.node_name)}</div>
                    <div class="stats-node-meta">${node.request_count} 次请求</div>
                </div>
                <div class="stats-node-tokens">${formatTokens(node.total_tokens)}</div>
            </div>
        `;
    }).join('');
}

async function refreshUsageStats() {
    setStatus('正在刷新统计...');
    try {
        await loadUsageStats();
        toast('统计数据已刷新');
        setStatus('就绪');
    } catch (err) {
        toast('刷新失败: ' + err, 'error');
        setStatus('刷新失败');
    }
}

// Settings Functions
function openConfigFolder(type) {
    const path = type === 'claude' ? '~/.apiclaude_config.json' : '~/.codex-api/';
    toast(`配置路径: ${path}`, 'success');
    // In a real implementation, this would call a Tauri command to open the folder
}

function exportSettings() {
    const exportData = {
        app_version: '1.0.0',
        exported_at: new Date().toISOString(),
        settings: settings,
        claude_nodes_count: Object.keys(claudeData.nodes || {}).length,
        codex_profiles_count: (codexData.profiles || []).length,
        // Sensitive data (tokens/keys) excluded
    };

    const blob = new Blob([JSON.stringify(exportData, null, 2)], { type: 'application/json' });
    const url = URL.createObjectURL(blob);
    const a = document.createElement('a');
    a.href = url;
    a.download = `api-manager-settings-${Date.now()}.json`;
    a.click();
    URL.revokeObjectURL(url);
    toast('配置已导出（不含敏感信息）');
}

function clearUsageData() {
    if (!confirm('确认清除所有使用记录？此操作不可撤销。')) return;

    // Clear last folder
    settings.lastFolder = null;
    saveSettings();

    toast('使用记录已清除');
}

function resetSettings() {
    if (!confirm('确认重置所有设置为默认值？此操作不可撤销。')) return;

    localStorage.removeItem('app_settings');
    settings = {
        defaultMode: 'new',
        defaultPerm: 'default',
        rememberFolder: false,
        autoRefresh: true,
        confirmDelete: true,
        sortBy: 'name',
        showFullToken: false,
        theme: 'light',
        lastFolder: null,
    };
    applySettings();
    toast('设置已重置');
}

function openExternal(url) {
    window.open(url, '_blank');
}

// Settings Change Handlers
document.getElementById('settingDefaultMode')?.addEventListener('change', (e) => {
    settings.defaultMode = e.target.value;
    saveSettings();
});

document.getElementById('settingDefaultPerm')?.addEventListener('change', (e) => {
    settings.defaultPerm = e.target.value;
    saveSettings();
});

document.getElementById('settingRememberFolder')?.addEventListener('change', (e) => {
    settings.rememberFolder = e.target.checked;
    saveSettings();
});

document.getElementById('settingAutoRefresh')?.addEventListener('change', (e) => {
    settings.autoRefresh = e.target.checked;
    saveSettings();
});

document.getElementById('settingConfirmDelete')?.addEventListener('change', (e) => {
    settings.confirmDelete = e.target.checked;
    saveSettings();
});

document.getElementById('settingSortBy')?.addEventListener('change', (e) => {
    settings.sortBy = e.target.value;
    saveSettings();
    render(document.getElementById('searchInput').value || '');
});

document.getElementById('settingShowFullToken')?.addEventListener('change', (e) => {
    settings.showFullToken = e.target.checked;
    saveSettings();
    render(document.getElementById('searchInput').value || '');
});

document.getElementById('settingTheme')?.addEventListener('change', (e) => {
    settings.theme = e.target.value;
    applyTheme(e.target.value);
    saveSettings();
});

loadSettings();
if (settings.autoRefresh) {
    loadData();
}
