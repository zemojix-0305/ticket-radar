/**
 * 运行配置 —— 一个小程序，两种后端
 * ---------------------------------------------------------------------------
 * USE_CLOUD = true   走微信云开发（云函数 radar）。这是线上/真机唯一能用的方式：
 *                    小程序不能依赖「开发者的电脑开着」，否则关掉电脑就全废。
 * USE_CLOUD = false  走 HTTP（server/api.js 或 Vercel）。用于没配云环境时本地调试。
 *
 * CLOUD_ENV：云开发环境 ID，在「微信开发者工具 → 云开发 → 设置」里能看到，
 * 形如 cloud1-2g3abcdefg。留空 = 用账号下的默认环境（只有一个环境时最省事，
 * 多环境的话一定要填，否则可能连到别人的测试环境上去）。
 */
const CLOUD_ENV = '';
const USE_CLOUD = true;

/** 云函数名（cloudfunctions/ 目录下的文件夹名） */
const CLOUD_FN = 'radar';

/** 回落用的 HTTP 地址：只在 USE_CLOUD=false 或云函数调不通时用 */
const API_BASE = '';

module.exports = { CLOUD_ENV, USE_CLOUD, CLOUD_FN, API_BASE };
