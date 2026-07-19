// 设置功能测试脚本
// 在浏览器控制台中运行此脚本来测试设置功能

console.log('=== 设置功能测试 ===\n');

// 1. 测试设置加载
console.log('1. 测试设置加载');
try {
    loadSettings();
    console.log('✓ 设置加载成功');
    console.log('当前设置:', settings);
} catch (e) {
    console.error('✗ 设置加载失败:', e);
}

// 2. 测试设置保存
console.log('\n2. 测试设置保存');
try {
    const originalMode = settings.defaultMode;
    settings.defaultMode = 'resume';
    saveSettings();

    // 重新加载验证
    const saved = JSON.parse(localStorage.getItem('app_settings'));
    if (saved.defaultMode === 'resume') {
        console.log('✓ 设置保存成功');
    } else {
        console.error('✗ 设置保存失败');
    }

    // 恢复原值
    settings.defaultMode = originalMode;
    saveSettings();
} catch (e) {
    console.error('✗ 设置保存失败:', e);
}

// 3. 测试设置应用到 UI
console.log('\n3. 测试设置应用到 UI');
try {
    applySettings();
    const modeSelect = document.getElementById('settingDefaultMode');
    if (modeSelect && modeSelect.value === settings.defaultMode) {
        console.log('✓ UI 同步成功');
    } else {
        console.error('✗ UI 同步失败');
    }
} catch (e) {
    console.error('✗ UI 同步失败:', e);
}

// 4. 测试导出功能
console.log('\n4. 测试导出功能');
try {
    // 模拟导出（不实际下载）
    const exportData = {
        app_version: '1.0.0',
        exported_at: new Date().toISOString(),
        settings: settings,
        claude_nodes_count: Object.keys(claudeData.nodes || {}).length,
        codex_profiles_count: (codexData.profiles || []).length,
    };

    if (exportData.settings && !exportData.settings.token && !exportData.settings.api_key) {
        console.log('✓ 导出数据格式正确，不包含敏感信息');
        console.log('导出数据预览:', exportData);
    } else {
        console.error('✗ 导出数据可能包含敏感信息');
    }
} catch (e) {
    console.error('✗ 导出功能失败:', e);
}

// 5. 测试设置项数量
console.log('\n5. 测试设置项完整性');
const expectedSettings = [
    'defaultMode',
    'defaultPerm',
    'rememberFolder',
    'autoRefresh',
    'confirmDelete',
    'sortBy',
    'showFullToken',
    'theme',
    'lastFolder'
];

const missingSettings = expectedSettings.filter(key => !(key in settings));
if (missingSettings.length === 0) {
    console.log('✓ 所有设置项都存在');
} else {
    console.error('✗ 缺少设置项:', missingSettings);
}

// 6. 测试 DOM 元素
console.log('\n6. 测试设置页 DOM 元素');
const settingsElements = [
    'settingDefaultMode',
    'settingDefaultPerm',
    'settingRememberFolder',
    'settingAutoRefresh',
    'settingConfirmDelete',
    'settingSortBy',
    'settingShowFullToken',
    'settingTheme'
];

let missingElements = [];
settingsElements.forEach(id => {
    if (!document.getElementById(id)) {
        missingElements.push(id);
    }
});

if (missingElements.length === 0) {
    console.log('✓ 所有 DOM 元素都存在');
} else {
    console.error('✗ 缺少 DOM 元素:', missingElements);
}

// 7. 测试函数是否挂载到 window
console.log('\n7. 测试全局函数');
const globalFunctions = [
    'openConfigFolder',
    'exportSettings',
    'clearUsageData',
    'resetSettings',
    'openExternal'
];

let missingFunctions = [];
globalFunctions.forEach(fn => {
    if (typeof window[fn] !== 'function') {
        missingFunctions.push(fn);
    }
});

if (missingFunctions.length === 0) {
    console.log('✓ 所有全局函数都已挂载');
} else {
    console.error('✗ 缺少全局函数:', missingFunctions);
}

console.log('\n=== 测试完成 ===');
console.log('提示: 在 Tauri 应用中运行此脚本以获得完整测试结果');
