const API = {
    async get(url) {
        const r = await fetch(url);
        if (!r.ok) throw new Error(`${r.status}`);
        return r.json();
    },
    async post(url, data) {
        const r = await fetch(url, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(data) });
        if (!r.ok) { const e = await r.json().catch(() => ({})); throw new Error(e.detail || r.status); }
        return r.json();
    },
    async del(url) {
        const r = await fetch(url, { method: 'DELETE' });
        if (!r.ok) { const e = await r.json().catch(() => ({})); throw new Error(e.detail || r.status); }
        return r.json();
    },
};

let currentView = 'claude';
let claudeData = { nodes: {}, current: null };
let codexData = { profiles: [] };
let selectedNode = null;

function guessProvider(url) {
    if (!url) return { name: 'API', cls: 'other' };
    const u = url.toLowerCase();
    if (u.includes('anthropic') || u.includes('claude')) return { name: 'Anthropic', cls: 'anthropic' };
    if (u.includes('openai')) return { name: 'OpenAI', cls: 'openai' };
    const m = u.match(/\/\/(?:www\.)?([^.\/]+)/);
    return { name: m ? m[1].charAt(0).toUpperCase() + m[1].slice(1) : 'API', cls: 'other' };
}

function shortUrl(url) {
    if (!url) return '-';
    try { const u = new URL(url); return u.host + (u.pathname !== '/' ? u.pathname : ''); }
    catch { return url; }
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

function updateClock() {
    const now = new Date();
    document.getElementById('statusTime').textContent =
        `本地时间: ${now.getFullYear()}-${String(now.getMonth()+1).padStart(2,'0')}-${String(now.getDate()).padStart(2,'0')} ${String(now.getHours()).padStart(2,'0')}:${String(now.getMinutes()).padStart(2,'0')}:${String(now.getSeconds()).padStart(2,'0')}`;
}
setInterval(updateClock, 1000);
updateClock();

// ─── Rendering ───

function renderClaudeView(filter = '') {
    const nodes = claudeData.nodes || {};
    const current = claudeData.current;
    const entries = Object.entries(nodes).filter(([n]) => !filter || n.toLowerCase().includes(filter.toLowerCase()));
    const count = entries.length;

    document.getElementById('claudeCurrentName').textContent = current || '-';
    document.getElementById('claudeCurrentStatus').textContent = current ? 'API 节点正在生效' : '未选择';
    document.getElementById('claudeNodeCount').textContent = count;
    document.getElementById('claudeAvailCount').textContent = count;
    document.getElementById('claudeFooterCount').textContent = `共 ${count} 个节点`;

    const tbody = document.getElementById('claudeTableBody');
    if (!count) {
        tbody.innerHTML = `<tr><td colspan="6" style="text-align:center;padding:40px;color:var(--text-3)">暂无 Claude 节点，点击「添加节点」创建</td></tr>`;
        return;
    }

    tbody.innerHTML = entries.map(([name, node]) => {
        const prov = guessProvider(node.base_url);
        const isCurrent = name === current;
        return `
        <tr class="${isCurrent ? 'current-row' : ''} ${selectedNode?.name === name ? 'selected' : ''}" data-name="${name}" data-type="claude" onclick="selectRow('claude','${name}')">
            <td><div class="node-name-cell"><span class="node-dot ${isCurrent ? 'active' : ''}"></span>${name}</div></td>
            <td><span class="provider-tag ${prov.cls}">${prov.name}</span></td>
            <td class="url-cell" title="${node.base_url || ''}">${shortUrl(node.base_url)}</td>
            <td class="token-cell">${node.token || '***'}</td>
            <td>${isCurrent ? '<span class="status-tag current">当前</span>' : '<span class="status-tag available">可用</span>'}</td>
            <td>
                <div class="row-actions">
                    <button class="row-btn primary" onclick="event.stopPropagation();launchNode('claude','${name}')" style="background:var(--green);border-color:var(--green)">启动</button>
                    ${isCurrent ? '' : `<button class="row-btn" onclick="event.stopPropagation();switchNode('claude','${name}')">切换</button>`}
                    <button class="row-btn danger" onclick="event.stopPropagation();deleteNode('claude','${name}')">删除</button>
                </div>
            </td>
        </tr>`;
    }).join('');
}

function renderCodexView(filter = '') {
    const profiles = codexData.profiles || [];
    const filtered = profiles.filter(p => !filter || (p.name || '').toLowerCase().includes(filter.toLowerCase()));
    const count = filtered.length;

    document.getElementById('codexProfileCount').textContent = count;
    document.getElementById('codexAvailCount').textContent = count;
    document.getElementById('codexFooterCount').textContent = `共 ${count} 个配置`;

    const sorted = [...filtered].sort((a, b) => (b.lastUsedAt || '').localeCompare(a.lastUsedAt || ''));
    if (sorted.length && sorted[0].lastUsedAt) {
        document.getElementById('codexCurrentName').textContent = sorted[0].name || sorted[0].id;
        document.getElementById('codexLastUsed').textContent = timeAgo(sorted[0].lastUsedAt);
        document.getElementById('codexLastUsedSub').textContent = new Date(sorted[0].lastUsedAt).toLocaleString('zh-CN');
    }

    const tbody = document.getElementById('codexTableBody');
    if (!count) {
        tbody.innerHTML = `<tr><td colspan="5" style="text-align:center;padding:40px;color:var(--text-3)">暂无 Codex 配置，点击「添加配置」创建</td></tr>`;
        return;
    }

    tbody.innerHTML = filtered.map(p => {
        const name = p.name || p.id;
        return `
        <tr class="${selectedNode?.name === name ? 'selected' : ''}" data-name="${name}" data-type="codex" onclick="selectRow('codex','${name}')">
            <td><div class="node-name-cell"><span class="node-dot"></span>${name}</div></td>
            <td class="url-cell" title="${p.baseUrl || ''}">${shortUrl(p.baseUrl)}</td>
            <td>${timeAgo(p.lastUsedAt)}</td>
            <td><span class="status-tag available">可用</span></td>
            <td>
                <div class="row-actions">
                    <button class="row-btn primary" onclick="event.stopPropagation();launchNode('codex','${name}')" style="background:var(--green);border-color:var(--green)">启动</button>
                    <button class="row-btn danger" onclick="event.stopPropagation();deleteNode('codex','${name}')">删除</button>
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
    const pct = total ? Math.round((claudeCount + codexCount) / Math.max(total, 1) * 100) : 0;
    document.getElementById('activeBar').style.width = pct + '%';
    document.getElementById('activeNodesLabel').textContent = `活跃: ${total}`;
}

// ─── Detail Panel ───

function selectRow(type, name) {
    selectedNode = { type, name };
    render();
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
        const profiles = codexData.profiles || [];
        const p = profiles.find(x => (x.name || x.id) === name);
        if (!p) return;
        document.getElementById('panelSub').textContent = 'Codex API 配置 · ID: ' + (p.id || name);
        document.getElementById('panelType').textContent = 'Codex CLI';
        document.getElementById('panelUrl').textContent = shortUrl(p.baseUrl);
        document.getElementById('panelToken').textContent = '***';
        document.getElementById('panelEndpoint').textContent = p.baseUrl || '-';
        document.getElementById('panelBadge').style.display = 'none';
        document.getElementById('panelSwitchBtn').textContent = '启动 Codex';
    }
}

// ─── Actions ───

async function loadData() {
    try {
        const [c, x] = await Promise.all([API.get('/api/claude/nodes'), API.get('/api/codex/profiles')]);
        claudeData = c;
        codexData = x;
        render();
        if (selectedNode) updatePanel();
        setStatus('数据加载成功');
    } catch (e) {
        setStatus('加载失败: ' + e.message);
    }
}

async function switchNode(type, name) {
    try {
        const r = await API.post(`/api/${type}/launch/${name}`, { args: [] });
        toast(r.message || `已切换到 ${name}`);
        await loadData();
    } catch (e) { toast(e.message, 'error'); }
}

async function deleteNode(type, name) {
    if (!confirm(`确认删除「${name}」？`)) return;
    try {
        const url = type === 'claude' ? `/api/claude/nodes/${name}` : `/api/codex/profiles/${name}`;
        await API.del(url);
        toast(`已删除「${name}」`);
        if (selectedNode?.name === name) selectedNode = null;
        await loadData();
    } catch (e) { toast(e.message, 'error'); }
}

let launchContext = { type: null, name: null, folder: null };

async function launchNode(type, name) {
    setStatus('正在选择文件夹...');
    try {
        // Step 1: Browse folder
        const r = await API.post('/api/browse-folder', {});
        if (!r.folder) {
            setStatus('已取消');
            return;
        }

        // Step 2: Store context and show launch config modal
        launchContext = { type, name, folder: r.folder };
        document.getElementById('launchFolder').value = r.folder;
        document.getElementById('launchModalTitle').textContent = type === 'claude' ? '启动 Claude 配置' : '启动 Codex 配置';

        // Show/hide Claude-specific options
        const isClaudeMode = type === 'claude';
        document.getElementById('launchModeField').style.display = isClaudeMode ? 'block' : 'none';
        document.getElementById('launchPermField').style.display = isClaudeMode ? 'block' : 'none';
        document.getElementById('launchModelField').style.display = isClaudeMode ? 'block' : 'none';

        // Reset radio buttons and select
        document.querySelector('input[name="launchMode"][value="new"]').checked = true;
        document.querySelector('input[name="launchPerm"][value="default"]').checked = true;
        document.getElementById('launchModel').value = '';

        document.getElementById('launchModal').classList.add('open');
        setStatus('等待启动配置...');
    } catch (e) {
        toast(e.message, 'error');
        setStatus('启动失败');
    }
}

async function reSelectFolder() {
    try {
        const r = await API.post('/api/browse-folder', {});
        if (r.folder) {
            launchContext.folder = r.folder;
            document.getElementById('launchFolder').value = r.folder;
        }
    } catch (e) { toast(e.message, 'error'); }
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

    console.log('[DEBUG] Launch params:', { type, name, folder, mode, permission, model, modelValue });

    setStatus('正在启动终端...');
    closeLaunchModal();

    try {
        if (type === 'claude') {
            console.log('[DEBUG] Sending to backend:', { folder, mode, permission, model });
            const r = await API.post(`/api/claude/start/${name}`, { folder, mode, permission, model });
            toast(r.message || '已启动 Claude');
        } else {
            const r = await API.post(`/api/codex/start/${name}`, { folder });
            toast(r.message || '已启动 Codex');
        }
        setStatus('启动成功');
    } catch (e) {
        toast(e.message, 'error');
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

// ─── Modal ───

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

async function submitForm(e) {
    e.preventDefault();
    const type = document.getElementById('addForm').dataset.type;
    const name = document.getElementById('fName').value.trim();
    const baseUrl = document.getElementById('fBaseUrl').value.trim();
    const token = document.getElementById('fToken').value.trim();
    const model = document.getElementById('fModel').value.trim();

    try {
        if (type === 'claude') {
            await API.post('/api/claude/nodes', { name, api_key: token, base_url: baseUrl || undefined });
        } else {
            await API.post('/api/codex/profiles', { name, api_key: token, base_url: baseUrl || undefined, model: model || undefined });
        }
        toast(`已添加「${name}」`);
        closeModal();
        await loadData();
    } catch (e) { toast(e.message, 'error'); }
}

// ─── UI Helpers ───

function toast(msg, type = 'success') {
    const old = document.querySelector('.toast-msg');
    if (old) old.remove();
    const el = document.createElement('div');
    el.className = 'toast-msg';
    el.style.cssText = `position:fixed;bottom:60px;right:24px;padding:10px 20px;background:${type==='error'?'var(--red)':'var(--green)'};color:#fff;border-radius:8px;font-size:13px;font-weight:500;z-index:2000;box-shadow:var(--shadow);animation:slideUp 0.25s ease-out`;
    el.textContent = msg;
    document.body.appendChild(el);
    setTimeout(() => el.remove(), 2500);
}

function setStatus(text) {
    document.getElementById('statusText').textContent = text;
}

// ─── Nav ───

document.querySelectorAll('.nav-item').forEach(item => {
    item.addEventListener('click', () => {
        const view = item.dataset.view;
        if (!view || view === 'settings') return;
        currentView = view;
        document.querySelectorAll('.nav-item').forEach(i => i.classList.remove('active'));
        item.classList.add('active');
        document.querySelectorAll('.view').forEach(v => v.classList.remove('active'));
        document.getElementById(view + '-view').classList.add('active');
    });
});

// Search
document.getElementById('searchInput').addEventListener('input', e => render(e.target.value));

// Add buttons
document.getElementById('addClaudeBtn').addEventListener('click', () => openModal('claude'));
document.getElementById('addCodexBtn').addEventListener('click', () => openModal('codex'));

// Quick switch
document.getElementById('quickSwitchBtn').addEventListener('click', () => {
    if (currentView === 'claude') openModal('claude');
    else openModal('codex');
});

// Modal overlay close
document.getElementById('modal').addEventListener('click', e => { if (e.target.id === 'modal') closeModal(); });

// Init
loadData();
