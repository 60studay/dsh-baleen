/* 鲸歌工具层 · 子进程环境（从 index.js 搬出来，纯搬移）。
 * 把插件自带的 pyserial、串口日志目录、场景目录一次性塞给 python 子进程，
 * 这样插件不依赖用户环境里装了什么。
 */

import { promises as fsp } from 'node:fs';
import path from 'node:path';

export function stripBom(text) {
  return text.charCodeAt(0) === 0xfeff ? text.slice(1) : text;
}

export async function readJson(file) {
  return JSON.parse(stripBom(await fsp.readFile(file, 'utf8')));
}


/** 组装 python 子进程的环境变量。vendor 由调用方传（插件根目录下的 pyserial）。 */
export function makeEnv({ logRoot, protocolRoot, vendor }) {
  const pythonPath = [vendor, process.env.PYTHONPATH].filter(Boolean).join(path.delimiter);
  return {
    ...process.env,
    PYTHONPATH: pythonPath,
    PYTHONIOENCODING: 'utf-8',
    PYTHONUTF8: '1',
    SERIAL_BRIDGE_LOG_DIR: logRoot,
    SERIAL_BRIDGE_PROTO_DIR: protocolRoot,
    SERIAL_BRIDGE_RUNTIME_DIR: path.join(logRoot, '.runtime'),
  };
}
