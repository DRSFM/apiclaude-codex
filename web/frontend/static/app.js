const API = {
    async get(url) {
        const r = await fetch(url);
        if (!r.ok) { const e = await r.json().catch(() => ({})); throw new Error(e.detail || r.status); }
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
let migrationData = {
    targets: [],
    threads: [],
    history: [],
    poolReady: false,
    sourceTargetId: null,
    targetTargetId: null,
    selectedThreadId: null,
    loading: false,
};

function escapeHtml(value) {
    return String(value ?? '')
        .replaceAll('&', '&amp;')
        .replaceAll('<', '&lt;')
        .replaceAll('>', '&gt;')
        .replaceAll('"', '&quot;')
        .replaceAll("'", '&#039;');
}

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

// ─── Conversation Migration ───

function migrationTargetName(target) {
    if (!target) return '尚未选择';
    return target.kind === 'account' ? '账号态 Codex' : target.name;
}

function migrationThreadStatus(status) {
    if (status === 'active') return '生成中';
    if (status === 'systemError') return '系统错误';
    return '空闲';
}

function migrationStatusClass(status) {
    if (status === 'active') return 'active';
    if (status === 'systemError') return 'error';
    return '';
}

function renderMigrationSourceOptions() {
    const select = document.getElementById('migrationSourceTarget');
    select.innerHTML = migrationData.targets.map(target => `
        <option value="${escapeHtml(target.id)}" ${target.id === migrationData.sourceTargetId ? 'selected' : ''} ${target.available ? '' : 'disabled'}>
            ${escapeHtml(migrationTargetName(target))}${target.model ? ` · ${escapeHtml(target.model)}` : ''}
        </option>
    `).join('');
}

function renderMigrationThreads() {
    const list = document.getElementById('migrationThreadList');
    const filter = document.getElementById('migrationThreadSearch').value.trim().toLowerCase();
    const threads = migrationData.threads.filter(thread => {
        if (!filter) return true;
        return [thread.title, thread.preview, thread.cwd, thread.id]
            .some(value => String(value || '').toLowerCase().includes(filter));
    });
    document.getElementById('migrationThreadCount').textContent = `${threads.length} 个会话`;
    if (!threads.length) {
        list.innerHTML = `<div class="migration-empty">${migrationData.loading ? '正在读取本地会话' : '没有符合条件的本地会话'}</div>`;
        return;
    }
    list.innerHTML = threads.map((thread, index) => `
        <button
            type="button"
            class="migration-thread-option ${thread.id === migrationData.selectedThreadId ? 'selected' : ''}"
            data-thread-index="${index}"
            aria-pressed="${thread.id === migrationData.selectedThreadId}"
            ${thread.available ? '' : 'disabled'}
        >
            <span class="migration-option-row">
                <span class="migration-option-title">${escapeHtml(thread.title || thread.id)}</span>
                <span class="migration-option-status ${migrationStatusClass(thread.status)}">${migrationThreadStatus(thread.status)}</span>
            </span>
            <span class="migration-option-preview">${escapeHtml(thread.preview || '无预览内容')}</span>
            <span class="migration-option-meta">${escapeHtml(thread.cwd || '未记录工作目录')} · ${escapeHtml(String(thread.id).slice(0, 12))}</span>
        </button>
    `).join('');
    list.querySelectorAll('.migration-thread-option').forEach(button => {
        button.addEventListener('click', () => {
            const thread = threads[Number(button.dataset.threadIndex)];
            migrationData.selectedThreadId = thread.id;
            renderMigrationThreads();
            updateMigrationSummary();
        });
    });
}

function renderMigrationTargets() {
    const list = document.getElementById('migrationTargetList');
    const targets = migrationData.targets.filter(
        target => target.id !== migrationData.sourceTargetId
    );
    if (!targets.length) {
        list.innerHTML = '<div class="migration-empty">没有其他可用目标</div>';
        return;
    }
    list.innerHTML = targets.map((target, index) => `
        <button
            type="button"
            class="migration-target-option ${target.id === migrationData.targetTargetId ? 'selected' : ''}"
            data-target-index="${index}"
            aria-pressed="${target.id === migrationData.targetTargetId}"
            ${target.available ? '' : 'disabled'}
        >
            <span class="migration-option-row">
                <span class="migration-option-title">${escapeHtml(migrationTargetName(target))}</span>
                <span class="migration-option-status ${target.available ? '' : 'error'}">${target.available ? '可用' : '目录缺失'}</span>
            </span>
            <span class="migration-option-preview">${escapeHtml(target.model || '跟随目标默认模型')}</span>
            <span class="migration-option-meta">${escapeHtml(target.modelProvider || 'Codex provider')}</span>
        </button>
    `).join('');
    list.querySelectorAll('.migration-target-option').forEach(button => {
        button.addEventListener('click', () => {
            const target = targets[Number(button.dataset.targetIndex)];
            migrationData.targetTargetId = target.id;
            renderMigrationTargets();
            updateMigrationSummary();
        });
    });
}

function renderMigrationHistory() {
    const tbody = document.getElementById('migrationHistoryBody');
    const history = migrationData.history || [];
    document.getElementById('migrationHistoryCount').textContent = `${history.length} 条记录`;
    if (!history.length) {
        tbody.innerHTML = '<tr><td colspan="5" class="migration-history-empty">暂无迁移记录</td></tr>';
        return;
    }
    tbody.innerHTML = history.slice(0, 12).map(item => {
        const main = item.refs?.main || Object.values(item.refs || {})[0] || {};
        const createdAt = item.createdAt ? new Date(item.createdAt).toLocaleString('zh-CN') : '-';
        return `
            <tr>
                <td title="${escapeHtml(item.title || '')}">${escapeHtml(item.title || '未命名会话')}</td>
                <td>${escapeHtml(item.name || '-')}</td>
                <td title="${escapeHtml(item.cwd || '')}">${escapeHtml(shortUrl(item.cwd || '-'))}</td>
                <td>${escapeHtml(String(main.commit || '-').slice(0, 12))}</td>
                <td>${escapeHtml(createdAt)}</td>
            </tr>
        `;
    }).join('');
}

function updateMigrationSummary() {
    const source = migrationData.targets.find(target => target.id === migrationData.sourceTargetId);
    const target = migrationData.targets.find(target => target.id === migrationData.targetTargetId);
    const thread = migrationData.threads.find(item => item.id === migrationData.selectedThreadId);

    document.getElementById('migrationSummarySource').textContent = migrationTargetName(source);
    document.getElementById('migrationSummaryThread').textContent = thread?.title || '尚未选择';
    document.getElementById('migrationSummaryTarget').textContent = migrationTargetName(target);
    document.getElementById('migrationSummaryModel').textContent = target?.model || '跟随目标配置';

    const ready = Boolean(
        migrationData.poolReady
        && thread?.available
        && target?.available
        && source
        && source.id !== target.id
        && !migrationData.loading
    );
    document.getElementById('startMigrationBtn').disabled = !ready;
    document.getElementById('startMigrationBtnInline').disabled = !ready;

    const result = document.getElementById('migrationResult');
    const hasPersistentResult = ['success', 'error', 'loading']
        .some(state => result.classList.contains(state));
    if (!hasPersistentResult) {
        showMigrationResult(
            '',
            ready ? '准备就绪' : '请选择来源会话和迁移目标',
            ready
                ? '确认后将清洗可见历史，并在目标中创建新的独立线程。'
                : '迁移后将在目标中生成新的线程 ID。'
        );
    }
}

async function loadMigrationThreads() {
    if (!migrationData.sourceTargetId) return;
    migrationData.loading = true;
    migrationData.selectedThreadId = null;
    renderMigrationThreads();
    updateMigrationSummary();
    try {
        const response = await API.get(`/api/share/threads?target_id=${encodeURIComponent(migrationData.sourceTargetId)}`);
        migrationData.threads = response.threads || [];
    } catch (e) {
        migrationData.threads = [];
        showMigrationResult('error', '无法读取来源会话', e.message);
    } finally {
        migrationData.loading = false;
        renderMigrationThreads();
        updateMigrationSummary();
    }
}

async function loadMigrationData() {
    migrationData.loading = true;
    setStatus('正在读取迁移配置...');
    updateMigrationSummary();
    try {
        const [targetResponse, historyResponse] = await Promise.all([
            API.get('/api/share/targets'),
            API.get('/api/share/history'),
        ]);
        migrationData.targets = targetResponse.targets || [];
        migrationData.history = historyResponse.conversations || [];
        migrationData.poolReady = Boolean(historyResponse.ready);
        const availableTargets = migrationData.targets.filter(target => target.available);
        if (!availableTargets.some(target => target.id === migrationData.sourceTargetId)) {
            migrationData.sourceTargetId =
                availableTargets.find(target => target.id === 'account')?.id
                || availableTargets[0]?.id
                || null;
        }
        if (
            !availableTargets.some(
                target => target.id === migrationData.targetTargetId
                    && target.id !== migrationData.sourceTargetId
            )
        ) {
            migrationData.targetTargetId =
                availableTargets.find(target => target.id !== migrationData.sourceTargetId)?.id
                || null;
        }
        const poolState = document.getElementById('migrationPoolState');
        poolState.textContent = historyResponse.ready
            ? `安全池已就绪 · ${historyResponse.pool}`
            : '共享池尚未初始化';
        poolState.classList.toggle('error', !historyResponse.ready);
        if (!historyResponse.ready && historyResponse.error) {
            showMigrationResult('error', '共享池不可用', historyResponse.error);
        }
        renderMigrationSourceOptions();
        renderMigrationTargets();
        renderMigrationHistory();
        await loadMigrationThreads();
        setStatus('迁移配置已刷新');
    } catch (e) {
        migrationData.poolReady = false;
        showMigrationResult('error', '迁移面板加载失败', e.message);
        setStatus('迁移面板加载失败');
    } finally {
        migrationData.loading = false;
        updateMigrationSummary();
    }
}

function showMigrationResult(type, title, detail) {
    const result = document.getElementById('migrationResult');
    result.classList.remove('success', 'error', 'loading');
    if (type) result.classList.add(type);
    result.innerHTML = `
        <span class="migration-result-label">${type === 'success' ? '迁移完成' : type === 'error' ? '需要处理' : '准备状态'}</span>
        <strong>${escapeHtml(title)}</strong>
        <p>${escapeHtml(detail)}</p>
    `;
}

async function startMigration() {
    const source = migrationData.targets.find(target => target.id === migrationData.sourceTargetId);
    const target = migrationData.targets.find(target => target.id === migrationData.targetTargetId);
    const thread = migrationData.threads.find(item => item.id === migrationData.selectedThreadId);
    if (!source || !target || !thread || migrationData.loading) return;

    migrationData.loading = true;
    updateMigrationSummary();
    showMigrationResult('loading', '正在创建安全副本', '正在清洗可见历史、发布版本并在目标中创建独立线程。');
    setStatus('会话迁移进行中...');
    try {
        const response = await API.post('/api/share/copy', {
            source_target_id: source.id,
            source_thread_id: thread.id,
            target_target_id: target.id,
        });
        showMigrationResult(
            'success',
            `已迁移到 ${migrationTargetName(target)}`,
            `新线程 ${response.target.threadId} · 共享版本 ${String(response.commit?.id || '').slice(0, 12)}`
        );
        toast('会话副本创建成功');
        const historyResponse = await API.get('/api/share/history');
        migrationData.history = historyResponse.conversations || [];
        renderMigrationHistory();
        setStatus('会话迁移完成');
    } catch (e) {
        showMigrationResult('error', '迁移未完成', e.message);
        toast(e.message, 'error');
        setStatus('会话迁移失败');
    } finally {
        migrationData.loading = false;
        updateMigrationSummary();
    }
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
        document.body.classList.toggle('migration-mode', view === 'migration');
        document.getElementById('searchInput').placeholder =
            view === 'migration' ? '搜索来源会话' : '搜索模型 / API 节点';
        document.getElementById('quickSwitchLabel').textContent =
            view === 'migration' ? '开始迁移' : '一键切换';
        if (view === 'migration' && !migrationData.targets.length) {
            loadMigrationData();
        }
    });
});

// Search
document.getElementById('searchInput').addEventListener('input', e => {
    if (currentView === 'migration') {
        document.getElementById('migrationThreadSearch').value = e.target.value;
        renderMigrationThreads();
    } else {
        render(e.target.value);
    }
});
document.getElementById('migrationThreadSearch').addEventListener('input', renderMigrationThreads);
document.getElementById('migrationSourceTarget').addEventListener('change', async event => {
    migrationData.sourceTargetId = event.target.value;
    if (migrationData.targetTargetId === migrationData.sourceTargetId) {
        migrationData.targetTargetId = migrationData.targets.find(
            target => target.available && target.id !== migrationData.sourceTargetId
        )?.id || null;
    }
    renderMigrationTargets();
    await loadMigrationThreads();
});

// Add buttons
document.getElementById('addClaudeBtn').addEventListener('click', () => openModal('claude'));
document.getElementById('addCodexBtn').addEventListener('click', () => openModal('codex'));

// Quick switch
document.getElementById('quickSwitchBtn').addEventListener('click', () => {
    if (currentView === 'migration') startMigration();
    else if (currentView === 'claude') openModal('claude');
    else openModal('codex');
});

// Modal overlay close
document.getElementById('modal').addEventListener('click', e => { if (e.target.id === 'modal') closeModal(); });

// Init
loadData();
