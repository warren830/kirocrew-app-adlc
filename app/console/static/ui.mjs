// The console's shared layer: transport, state context and UI primitives (pages import these; they never fetch).
import React from 'react'

const { useState, useEffect, useCallback, useContext, createContext, createElement: h } = React

// ---------------------------------------------------------------------------
// Transport: every platform call goes through call() / events(); pages never fetch themselves
// ---------------------------------------------------------------------------

// Where the console is, as the page that serves it writes it (index.html): on its own server the API is /api/console;
// inside KiroCrew it is /apps/workshop-customizer/api/console, the App's backend behind the gateway's signed proxy.
const meta = (name, fallback) => (document.querySelector(`meta[name="${name}"]`) || {}).content || fallback
const API = meta('adlc-api', '/api/console')
const HOST = meta('adlc-host', 'standalone')
export const IN_KIROCREW = HOST === 'kirocrew'

// Inside KiroCrew the 工作坊 item opens KiroCrew's own Workshop Customizer view: this frame's parent, the App's page.
export function openWorkshop() {
  window.parent.postMessage({ type: 'adlc-console:open', view: 'workshop' }, window.location.origin)
}

async function read(response) {
  const text = await response.text()
  let data = null
  try { data = text ? JSON.parse(text) : null } catch (_) { data = { error: text.slice(0, 300) } }
  return data
}

// The workspace's AWS credentials, by what botocore or AWS said: a profile that does not exist, a login that ran out,
// credentials AWS does not accept, none at all. null when the message is about something else.
export function credentialText(said) {
  const text = String(said || '')
  const named = /AWS profile '([^']*)' is not usable from the console/.exec(text)
  const missing = /config profile \(([^)]+)\) could not be found/.exec(text)
  const profile = (named && named[1] !== '?' && named[1]) || (missing && missing[1]) || ''
  const who = profile ? `本机 AWS profile「${profile}」` : '这个工作区'
  const other = /workspace (\S+) resolves to account (\d+), not (\d+)/.exec(text)
  if (other) return `这个工作区的凭证登进的是账号 ${other[2]}，不是填的 ${other[3]}：换成账号 ${other[3]} 的 profile 或角色，或者把「账号」改成 ${other[2]}，保存后点「核对」。`
  if (/ProfileNotFound/.test(text) || missing) {
    return `这台机器的 ~/.aws/config 里没有 profile「${profile || '?'}」：在「设置 → 工作区」把「本机 AWS profile」改成存在的名字（终端运行 aws configure list-profiles 能列出来），保存后点「核对」。`
  }
  if (/ExpiredToken|RequestExpired|TokenRetrievalError|SSOTokenLoadError|UnauthorizedSSOTokenError|token (?:included in the request )?(?:is|has) expired/i.test(text)) {
    return `${who}的${profile ? '' : ' AWS '}登录过期了：在终端运行 aws sso login${profile ? ` --profile ${profile}` : ''}（或换上新的临时凭证），再到「设置 → 工作区」点「核对」。`
  }
  if (/InvalidClientTokenId|UnrecognizedClientException|security token included in the request is invalid/.test(text)) {
    return `AWS 不认${who}用的密钥（无效或已经吊销）：换一组有效的凭证，再到「设置 → 工作区」点「核对」。`
  }
  if (/NoRegionError/.test(text)) return '没有区域：在「设置 → 工作区」给这个工作区填上「区域」（例：us-west-2），保存后点「核对」。'
  if (/NoCredentialsError|Unable to locate credentials|PartialCredentialsError|CredentialRetrievalError/.test(text) || named) {
    return `${who}找不到能用的 AWS 凭证：先登录（aws sso login，或配好 access key），再到「设置 → 工作区」点「核对」。`
  }
  return null
}

// What AWS's own refusal codes mean (the console answers them as "<Code>: <AWS's message>").
const AWS_CODES = {
  AccessDeniedException: '没有权限：这个工作区用的角色或 profile 缺这个权限', AccessDenied: '没有权限：这个工作区用的角色或 profile 缺这个权限',
  UnauthorizedOperation: '没有权限：这个工作区用的角色或 profile 缺这个权限', ValidationException: 'AWS 认为参数不对',
  ResourceNotFoundException: '要找的资源不存在（也许已经删了）', ConflictException: '和现有的资源冲突（也许它正在创建或删除）',
  ThrottlingException: 'AWS 限流了：过一会儿再试', TooManyRequestsException: 'AWS 限流了：过一会儿再试',
  ServiceQuotaExceededException: '超出了这个账号的服务配额', LimitExceededException: '超出了这个账号的服务配额',
  InternalServerException: 'AWS 内部出错：过一会儿再试', ServiceUnavailableException: 'AWS 暂时不可用：过一会儿再试',
}

// The autopilot loop's words for a round, a prediction and a direct verdict (tools/kiro_generate.py).
const ROUND_MODE = { draft: '起草', repair: '修复', regenerate: '改写' }
const PREDICTED = { likely_ready: '预计能上课', likely_not_ready: '预计还不能上课', unknown: '预测不了' }
function directVerdict(v) {
  const flaky = /^ready but not robust \((.*)\)$/.exec(v)
  return flaky ? `不稳定：${flaky[1]} 有的轮次没复现` : { not_ready: '没通过', insufficient_evidence: '证据不足', ready: '通过' }[v] || v
}
// A rehearsal's verdict reason (rehearsal.py reasonCode), in the 工作坊's words (rehearsal_strings, zh).
const REASON_CODE = {
  TEACHING_UNDECLARED: '这次构建没有声明教学设计（labs.teaching）', PARITY_MISSING: '缺少决定性类型的教学现象',
  RUN_INCOMPLETE: '这个发布包还没有一次完整运行', CONTRASTS_REPRODUCED: '所有决定性的教学对比都复现了',
  PHENOMENON_NOT_REPRODUCED: '有决定性的教学对比没复现', PHENOMENON_INSUFFICIENT: '证据不足以判定全部教学对比',
  REPRODUCED: '复现了', VERDICT_NOT_READY: '有决定性的教学对比没复现', VERDICT_INSUFFICIENT_EVIDENCE: '证据不足以判定全部教学对比',
}
export const reasonText = (code) => (!code || code === 'None' ? '' : REASON_CODE[code] ? `${REASON_CODE[code]}（${code}）` : `原因代码 ${code}`)
// A rehearsal's remediation codes (rehearsal.py hints), in the 工作坊's words (rehearsal_strings zh, without their figures).
const HINT_CODE = {
  PF_BASELINE_ALREADY_PASSES: '基线回答已经有据，声明的基线缺陷没起作用', PF_RETRIEVAL_FAILED: '检索来源没覆盖问题，这是检索问题，改提示词修不好',
  PF_NO_GAIN: '优化后的 GR 提升没超过噪声带', PF_OPTIMIZED_NOT_PASS: '优化后的回答有提升，但 THELMA 仍没判通过', PF_METRICS_MISSING: 'THELMA 没有返回这道题的 SQC',
  RG_RETRIEVAL_OK: '检索找到了答案，构不成检索缺口', RG_OPTIMIZED_RESOLVED: '两轮检索都失败，但优化后的回答没被判为 Fail', RG_METRICS_MISSING: 'THELMA 既没返回 SP2，也没返回 SQC',
  RG_ABSENT_NOT_SCORED: '检索什么都没返回，THELMA 跳过了这道题', RG_ABSENT_TOPIC_COVERED: '问的是文档里已有的主题，构不成「答案不在知识库里」',
  RG_REDECLARE: '这道题的缺口类型也许声明错了', L1_BASELINE_ALREADY_PASSES: '基线已经通过关键判据，对比做不出来', L1_OPTIMIZED_FAILS: '优化轮没通过关键判据',
  L1_UNRESOLVED: 'Mind the Goal 没给出结论', L1_EMPTY_RESPONSE: '优化轮的回答是空的', L1_ERROR: 'L1 判这道题时出错', L1_MISSING: '没有这道题的 L1 结论',
  L1_NO_FOCUS_CHECK: '这道题没有声明关键判据', NOT_SCORED: '这道题没有 THELMA 分数', NOISE_BAND_MISSING: '没有裁判噪声带',
  JUDGE_TOO_NOISY: '裁判噪声带太大，THELMA 区分不了两轮', CASE_NOT_PRACTICE: '这道题不是本次构建的练习题', UNSUPPORTED_KIND: '未知的教学现象类型',
  RELEASE_NOT_VERIFIED: '确认不了这次构建就是彩排用的发布包',
}
// The loop's blocking remediation ("CODE (caseId), …"): each in words, with the practice question it is about.
export const blockingText = (list) => String(list || '').split(', ').filter(Boolean).map((item) => {
  const m = /^(\S+) \((.*)\)$/.exec(item) || [item, item, '']
  const cid = m[2] && m[2] !== '—' ? m[2] : ''
  return HINT_CODE[m[1]] ? `${HINT_CODE[m[1]]}（${m[1]}${cid ? `，题目 ${cid}` : ''}）` : `${m[1]}${cid ? `（题目 ${cid}）` : ''}`
}).join('；')
// What Kiro CLI said when it failed a round (the loop's round errors, its staged stages' errors), with what to do.
function kiroText(said) {
  let m = /Kiro CLI timed out after (\d+)s/.exec(said)
  if (m) return `Kiro CLI 跑了 ${secs(m[1])}还没结束，超时了：换一个项目 ID 再开始一次；一直超时的话，把简报写短一点。`
  if (/not logged in|log ?in|unauthori[sz]ed|authenticat|token (?:has )?expired|session expired/i.test(said)) {
    return 'Kiro CLI 没登录或登录过期：在终端运行 kiro-cli login，再到工作坊页打开这个项目接着修，或换一个项目 ID 重新开始。'
  }
  m = /^Kiro CLI could not run: (.*)$/s.exec(said)
  if (m) return `Kiro CLI 启动不了（${m[1]}）：在终端运行 kiro-cli 看看，没登录的话先 kiro-cli login。`
  m = /(?:KiroExit: )?exit (-?\d+): ?(.*)$/s.exec(said)
  if (m) return `Kiro CLI 出错退出（退出码 ${m[1]}${m[2].trim() ? `：${m[2].trim()}` : ''}）：在终端运行 kiro-cli 看它能不能正常对话，再换一个项目 ID 重新开始。`
  return said
}
// Why the 工作坊 did not take a round of Kiro's (app/backend/routes.py GenerationError), with what to do.
function generationText(said) {
  let m = /^generated file contains credential-shaped material: (.*)$/.exec(said)
  if (m) return `Kiro 写出的 ${m[1]} 里有像密钥的内容，为安全没有应用：检查简报和资料里有没有贴密钥，删掉后换一个项目 ID 重新开始。`
  if (/^generated scenario contains credential-shaped material/.test(said)) return 'Kiro 写出的场景里有像密钥的内容，为安全没有应用：检查简报和资料里有没有贴密钥，删掉后换一个项目 ID 重新开始。'
  if (/^remove credential-shaped material before sending/.test(said)) return '简报或资料里有像密钥的内容（AWS 密钥、令牌之类）：删掉后换一个项目 ID 重新开始。'
  m = /^background job (\S+) is running for this project/.exec(said)
  if (m) return `这个项目有别的操作在跑（${m[1]}，例如工作坊页的构建）：等它结束，再换一个项目 ID 开始。`
  if (/a Kiro generation is already running for this project|an operation is already running/.test(said)) return '这个项目已经有别的操作在跑（例如工作坊页的生成或构建）：等它结束，再换一个项目 ID 开始。'
  m = /^Kiro's reply was cut off after (\d+) characters/.exec(said)
  if (m) return `Kiro 的回复写到 ${m[1]} 个字就断了：换一个项目 ID 重新开始；反复出现就把简报写短一点。`
  if (/^Kiro returned (?:no JSON generation object|invalid JSON)|^Kiro generation output must be a JSON object/.test(said)) return 'Kiro 的回复不是合格的 JSON：换一个项目 ID 重新开始。'
  m = /^generated scenario references missing files: (.*)$/.exec(said)
  if (m) return `Kiro 写的场景引用了它没写出来的文件（${m[1]}），没有应用：换一个项目 ID 重新开始；反复出现就把原文发给维护者。`
  return `Kiro 这一轮写出的内容没通过工作坊的检查，没有应用（${said}）：换一个项目 ID 重新开始；反复出现就把原文发给维护者。`
}
// The autopilot loop's stop that failed its job (exits 2 and 3, or no report): which step stopped it, then why.
const LOOP_STEPS = [
  [/^pre-rehearsal (\d+) failed: (.*)$/s, (m) => [`第 ${m[1]} 次本地预测调 AWS / Bedrock 时出错`, m[2], 'aws']],
  [/^direct (\d+) failed: (.*)$/s, (m) => [`第 ${m[1]} 次快速彩排调 AWS 时出错`, m[2], 'direct']],
  [/^direct: the AWS profile resolves to another account than --aws-account/, () => ['设置有误', '工作区的 AWS profile 登进的不是工作区填的账号：到「设置 → 工作区」点「核对」，换成对的 profile 或账号。']],
  [/^round (\d+): (.*)$/s, (m) => [`第 ${m[1]} 轮 Kiro CLI 出错`, kiroText(m[2])]],
  [/^refusing the live KiroCrew App data dir/, () => ['设置有误', '自动驾驶没被允许写 KiroCrew App 自己的数据目录：这是控制台的问题，把这行原文发给维护者。']],
  [/^engine unavailable: (.*)$/s, (m) => ['引擎加载不了', m[1]]],
  [/^the task sent differs from the recorded task/, () => ['内部检查没过', '发给 Kiro 的任务和记录的不一致：再开始一次；还这样，把任务日志发给维护者。']],
]
const STOPPED_BY = { 2: '被拒绝，或设置有误', 3: 'Kiro CLI 或 AWS 出错' }
const AFTER_WAIT = '这次自动驾驶已经停了：到 AgentCore 控制台看它为什么没就绪（多半是权限或配额），再到工作坊页打开这个项目重新做快速彩排。'
const AFTER_AWS = '这次自动驾驶已经停了：处理好上面的原因后，到工作坊页打开这个项目接着做，或换一个项目 ID 重新开始。'
// A job step that gave up waiting for an AWS resource: the job has stopped, a refresh does not finish it.
const WAIT_TAIL = '：这一步没等到它就绪就停了。到 AgentCore 控制台看它为什么没就绪（多半是权限或配额），处理好后重新执行这一步。'
// A loop process ended by a signal (a negative exit): who ended it, as far as the console can tell.
const SIGNALLED = { '-9': '进程被强制结束了（退出码 -9，多半是内存不够或被手动结束）', '-15': '进程被停止了（退出码 -15，被手动结束，或者机器在关机）' }
function loopStop(code, said) {
  const repair = /^(pre-rehearsal|direct) (\d+) repair: /.exec(said)
  const rest = repair ? said.slice(repair[0].length) : said
  const during = repair ? `，在按第 ${repair[2]} 次${repair[1] === 'direct' ? '快速彩排' : '本地预测'}的意见修复时` : ''
  for (const [re, out] of LOOP_STEPS) {
    const m = re.exec(rest)
    if (!m) continue
    const [step, why, aws] = out(m)
    const cred = credentialText(why)
    if (cred || !aws) return `自动驾驶停了（${step}${during}）：${cred || consoleText(why) || why}`
    // an AWS step's own words ("ProvisionError: Gateway … is FAILED"), then what to do: the run has stopped, no refresh helps
    const bare = why.replace(/^[A-Z]\w*(?:Error|Exception): /, '')
    const said = (consoleText(bare) || consoleText(why) || bare).replace(WAIT_TAIL, '。').replace(/：等它就绪再试。$/, '。')
    const waited = aws === 'direct' && /还是「|的状态是「/.test(said)
    return `自动驾驶停了（${step}${during}）：${said}${/[。.]$/.test(said) ? '' : '。'}${waited ? AFTER_WAIT : AFTER_AWS}`
  }
  if (/^-\d+$/.test(code)) return `自动驾驶停了：${SIGNALLED[code] || `进程被信号 ${code.slice(1)} 结束了（退出码 ${code}）`}。到工作坊页打开这个项目接着做，或换一个项目 ID 重新开始。`
  const by = STOPPED_BY[code] || `退出码 ${code}`
  return rest === 'see the job log' ? `自动驾驶停了（${by}）：原因在上面的任务日志里。` : `自动驾驶停了（${by}${during}）：${credentialText(rest) || consoleText(rest) || rest}`
}

// CodeBuild's build phases and statuses.
export const BUILD_PHASE = { IN_PROGRESS: '构建中', QUEUED: '排队中', SUBMITTED: '已提交', PROVISIONING: '准备构建环境', DOWNLOAD_SOURCE: '下载源码', INSTALL: '安装依赖',
  PRE_BUILD: '构建前准备', BUILD: '构建中', POST_BUILD: '构建后处理', UPLOAD_ARTIFACTS: '上传产物', FINALIZING: '收尾' }
// CodeBuild's phases by name (where a build failed).
export const PHASE_NAME = { SUBMITTED: '提交', QUEUED: '排队', PROVISIONING: '准备构建环境', DOWNLOAD_SOURCE: '下载源码', INSTALL: '安装依赖', PRE_BUILD: '构建前准备',
  BUILD: '构建', POST_BUILD: '构建后处理', UPLOAD_ARTIFACTS: '上传产物', FINALIZING: '收尾' }
// What a failed phase most likely means (deploy.py's buildspecs: ECR login in pre_build, pip or docker in build).
const PHASE_HINT = { BUILD: '多半是 requirements.txt 里有的包没有 linux/arm64 版本、装不上；用 Dockerfile 构建的话，看 Dockerfile 哪一步出错',
  PRE_BUILD: '多半是 CodeBuild 的角色登录不了 ECR（缺权限）', DOWNLOAD_SOURCE: 'CodeBuild 读不到控制台 S3 桶里的源码（多半是权限）',
  PROVISIONING: 'CodeBuild 准备不了构建环境（多半是账号的 CodeBuild 配额或它的角色）' }
// The console's own states of an experiment or a canary (lowercase), else AWS's.
const RUN_STATE = { creating: '创建中', running: '运行中', paused: '已暂停', stopped: '已停止', promoted: '已发布', failed: '失败', cleaning: '清理中',
  cleaned: '已清理', promoting: '发布中', rolling_back: '回滚中', rolled_back: '已回滚', cleanup_incomplete: '清理未完成' }
const stateText = (s) => RUN_STATE[s] || statusText(s)
// What an AWS wait names ("A/B test …", "endpoint …", "Gateway …", "Harness …"), in the page's words.
const subject = (s) => s.replace(/^the /, '').replace(/^A\/B test /, 'A/B 实验 ').replace(/^endpoint /, '端点 ').replace(/^(?:Gateway )?target /, 'Gateway 目标 ')
  .replace(/^online evaluation /, '在线评估 ').replace(/^memory /, '记忆（Memory）')

// What a backend refusal means, in the page's words. The backend answers in English (its tests and the API's other
// callers read that); the page says it in Chinese and keeps the original (`said`) as the error line's tooltip. An entry
// whose function answers null lets the next one try.
const CONSOLE_ERRORS = [
  // a canary's promotion / rollback / setup / cleanup that did not finish, and an experiment's cut-off job (first: their
  // inner AWS errors would otherwise be taken for the whole)
  [/^(promotion|rollback|setup|cleanup): (.*)$/s, (m) => phaseErrorText(m[1], m[2])],
  [/^the (setup|cleanup) job (\S+ did not finish .*)$/s, (m) => phaseErrorText(m[1] === 'setup' ? 'experiment' : 'cleanup', `the job ${m[2]}`)],
  [/^promotion refused: /, '门禁没通过，不能发布：没通过的条件见门禁卡（证据不足也要发布，就勾选管理员覆盖并写原因）。'],
  [/^above (\d+) % only once the gate holds/, (m) => `候选流量超过 ${m[1]}% 要等门禁通过（否则大部分会话用的是没有证据的候选）：先看门禁卡还差哪一条。`],
  [/id must be lowercase kebab-case/, 'ID 要字母开头，只用小写字母、数字和连字符，2–40 个字符（例：prod-us-west-2）。'],
  [/accountId must be 12 digits/, '「账号」要填 12 位数字的 AWS 账号 ID。'],
  [/region must look like/, '「区域」要写成 us-west-2 这样的格式。'],
  [/roleArn must be an IAM role ARN/, '跨账号角色 ARN 要是一个 IAM 角色的 ARN（arn:aws:iam::<12 位账号>:role/…）。'],
  [/roleArn must be a role in the workspace's account/, '跨账号角色要在这个工作区的账号里：ARN 里的 12 位数字要和「账号」一样。'],
  [/give a local AWS profile, or the spoke role to assume/, '「本机 AWS profile」和「跨账号角色 ARN」至少填一个。'],
  [/permissionsBoundaryArn must be an IAM managed policy ARN/, '权限边界 ARN 要是一个 IAM 托管策略的 ARN（spoke-role.yaml 输出的 PermissionsBoundaryArn）。'],
  [/permissionsBoundaryArn must be a policy in the workspace's account/, '权限边界要在这个工作区的账号里：ARN 里的 12 位数字要和「账号」一样。'],
  [/^no workspace\s*$/, '先选一个工作区：还没有的话，到「设置 → 工作区」添加。'],
  [/^no workspace (\S+)/, (m) => `没有工作区 ${m[1]}（也许已经移除）：到「设置 → 工作区」看看。`],
  [/username must be 2-32/, '用户名要 2–32 个字符：小写字母、数字、点、下划线或连字符。'],
  [/role must be admin or member/, '角色只能是管理员或成员。'],
  [/a new user needs a password/, '新用户要设一个至少 10 位的密码。'],
  [/a password needs at least 10 characters/, '密码至少要 10 位。'],
  [/the first user must be an admin/, '第一个用户必须是管理员：把「角色」选成管理员。'],
  [/keep at least one admin/, '至少要留一个管理员：先把另一个用户设成管理员。'],
  [/the last admin cannot be removed/, '最后一个管理员不能删（删了控制台会重新对所有人开放）：先把另一个用户设成管理员。'],
  [/^no user (\S+)/, (m) => `没有用户 ${m[1]}。`],
  [/wrong username or password/, '用户名或密码不对。'],
  [/^sign in first/, '先登录：登录也许过期了，刷新页面再登录。'],
  [/no access to workspace (\S+)/, (m) => `你没有工作区 ${m[1]} 的权限：请管理员在「设置 → 用户与权限」里加上。`],
  [/^(dataset )?name: a letter, then letters, digits or underscores(?: \(at most (\d+)\))?/, (m) => `${m[1] ? '数据集名称' : '名称'}要字母开头，只用字母、数字或下划线${m[2] ? `（最多 ${m[2]} 个字符）` : ''}。`],
  [/^(?:endpoint|name): (?:a name|an endpoint name) \(a letter, then letters, digits or underscores\), not DEFAULT/, '端点名要字母开头，只用字母、数字或下划线，也不能叫 DEFAULT。'],
  [/the frontmatter name must be (\S+)/, (m) => `SKILL.md 开头（frontmatter）的 name 要和技能名一样：把 name: 改成 ${m[1]}。`],
  [/^(.+?) was not created from this console: send acknowledged: true/, (m) => `${m[1]} 不是这个控制台创建的：要改它得先在确认框里确认。`],
  [/an online evaluation runs, and is billed, for every sampled session until deleted/, '在线评估会对每个抽样的会话持续评估并计费，直到删除：要先确认。'],
  [/^projectId: lowercase letters/, '项目 ID 只能用小写字母、数字和连字符，字母开头，2–48 个字符（例：acme-claims）。'],
  [/^brief: the customer's situation/, '客户简报要 200 到 60 000 字：谁在用、问什么、背后的系统和规则。'],
  [/autopilot needs a workspace reached through a local AWS profile/, '自动驾驶要用本机 AWS profile 直接连的工作区：它在自己的进程里跑一个小时左右，跨账号角色的临时凭证撑不到结束。到「设置 → 工作区」添加一个用本机 profile 直接登进这个账号的工作区（跨账号角色 ARN 留空），切换过去再开始；或者把「构建之后」选成「都不做」（完全不用 AWS）。'],
  [/kiro-cli is not on this machine's PATH/, '这台机器上找不到 kiro-cli：先安装并登录（kiro-cli login）。'],
  [/^project (\S+) already exists in the workshop data/, (m) => `工作坊里已经有项目 ${m[1]}（也许是之前一次自动驾驶写的，工作坊页能打开它）：换一个项目 ID 再开始。`],
  [/^an autopilot is already running \((\S+)\)/, '已经有一个自动驾驶在运行：Kiro 和直连资源是共用的，一次只能跑一个，等它结束。'],
  [/^the validation is not ok; build skipped/, '校验没通过，没有构建：到工作坊页「02 → 教学与评估」看还差什么（「客户严格确认」的项目要先在那里由客户确认阻断项）。'],
  [/^build refused: (.*)$/s, (m) => `构建被拒绝（${m[1]}）：到工作坊页「02 → 教学与评估」看还差什么。`],
  [/the loop exited (-?\d+): (.*)$/s, (m) => loopStop(m[1], m[2])],
  // the autopilot loop's content stops (tools/kiro_generate.py exits 1, 4 and 5: the run's outcome), with what to do next
  [/^(pre-rehearsal|direct) (\d+) repair: (.*)$/s, (m) => `按第 ${m[2]} 次${m[1] === 'direct' ? '快速彩排' : '本地预测'}的意见修复时停了：${consoleText(m[3]) || m[3]}`],
  [/^Kiro needs input: ?(.*)$/s, (m) => `Kiro 要先知道这些才能写下去：${m[1].trim() ? m[1].trim().split('; ').map((q) => `「${q}」`).join('') : '（它没列出问题）'}。到工作坊页打开这个项目回答（会带着回答重新起草），或者把答案写进简报，换一个项目 ID 再开始。`],
  [/^Kiro still needs input after the answers/, 'Kiro 拿到回答后还要更多信息：到工作坊页打开这个项目，看它还问什么。'],
  [/^repairable errors or live-learned teaching warnings remain after (\d+) rounds: (.*)$/s, (m) => `Kiro 修了 ${m[1]} 轮，校验还剩它没修好的问题（${m[2]}）：到工作坊页打开这个项目看还差什么，在那里接着修。`],
  [/^no progress: the same findings remain after an escalation to regenerate: (.*)$/s, (m) => `Kiro 换成改写后还是同样的问题（${m[1]}），没有进展就停了：到工作坊页打开这个项目看这些问题，多半要在简报或资料里补充信息。`],
  [/^pre-rehearsal findings remain: pre-rehearsal (\d+) \((\w+)\) still finds (\d+) after (\d+) repair\(s\)/, (m) => `本地预测修了 ${m[4]} 次，第 ${m[1]} 次还找到 ${m[3]} 个问题（${PREDICTED[m[2]] || m[2]}），所以没有上 AWS 彩排：问题列在下面的「本地预测」里，到工作坊页打开这个项目修。`],
  [/^direct rehearsal (\d+) is (.*?) \(([^)]*)\) after (\d+) repair\(s\): (.*)$/s, (m) => {
    const flaky = /^ready but not robust \((.*)\)$/.exec(m[2])
    const why = flaky ? '' : reasonText(m[3])  // a flaky run's reason is its decisive round's: that one held
    const head = `快速彩排修了 ${m[4]} 次，第 ${m[1]} 次还是${directVerdict(m[2])}${why ? `：${why}` : ''}`
    if (m[5] !== 'no blocking remediation') return `${head}。要改的有：${blockingText(m[5])}。到工作坊页打开这个项目，按彩排报告的整改意见修。`
    return flaky ? `${head}。没有要改的内容：不稳定多半来自模型回答的随机性。到工作坊页重新彩排；还不稳定，就把 ${flaky[1]} 的问法或评分标准写得更明确。`
      : `${head}，整改意见里没有阻断项：到工作坊页打开这个项目看彩排报告。`
  }],
  [/^direct rehearsal (\d+) is (\w+) and names no project asset to repair \((.*)\)$/s, (m) => (m[3] === 'environment hints only'
    ? `第 ${m[1]} 次快速彩排${directVerdict(m[2])}，整改意见只有环境方面的提示：按彩排报告的提示检查 AWS 环境，再到工作坊页重新彩排。`
    : `第 ${m[1]} 次快速彩排${directVerdict(m[2])}，但这些整改项对不上项目里能改的文件，Kiro 没法自动修：${blockingText(m[3])}。到工作坊页看彩排报告，手动调整后重新彩排。`)],
  [/^round (\d+) \((\w+)\) refused: (.*)$/s, (m) => `第 ${m[1]} 轮（${ROUND_MODE[m[2]] || m[2]}）没能开始：${generationText(m[3])}`],
  [/^round (\d+): apply refused: (.*)$/s, (m) => `第 ${m[1]} 轮 Kiro 的改动没能应用：${generationText(m[2])}`],
  [/^round (\d+): (.*)$/s, (m) => `第 ${m[1]} 轮 Kiro 的输出用不了，重跑也没成：${generationText(m[2])}`],
  [/^materials refused: (.*)$/s, (m) => `资料没能上传：${consoleText(m[1]) || m[1]}`],
  [/^batch review refused: (.*)$/s, (m) => `内部参考包的批量确认被拒绝：${consoleText(m[1]) || m[1]}`],
  [/^app refused: (.*)$/s, (m) => `工作坊的后端拒绝了这一步：${consoleText(m[1]) || m[1]}`],
  [/^the project carries manual edits or orphans/, '项目里有手动改过的内容或多出来的文件，这次结果不算 Kiro 独立完成的：到工作坊页打开这个项目看是哪些。'],
  [/the console restarted while this job ran/, '这个任务运行时控制台重启了，任务被中断：重新开始一次。'],
  // contracts (direct/verify.check_contracts)
  [/^contract (\d+) is not a mapping/, (m) => `第 ${m[1]} 条契约要是一个对象：{"id": …, "query": …, "expected": {…}}。`],
  [/^contract (\d+): id '(.*)' is not unique: contract (\d+) has it too/, (m) => `第 ${m[1]} 条契约的 id「${m[2]}」和第 ${m[3]} 条重复：换一个 id。`],
  [/^contract (\S+): expected checks nothing/, (m) => `契约 ${m[1]} 没有能检查的项：false 和空列表都不算检查（该不该拒绝或转人工，由目标裁判判断），这样的契约永远过不了。至少给一项非空的 mustMention、mustMentionAnyOf、mustNotMention、requiredTools、forbiddenTools，或把 shouldRefuse / shouldEscalate 设成 true。`],
  [/^contract (\d+): id must be a unique short identifier, not (.*)$/, (m) => `第 ${m[1]} 条契约的 id 要是不重复的短标识（字母或数字开头，只用字母、数字和 _ . : -），现在是 ${m[2]}。`],
  [/^contract (\S+): query is empty/, (m) => `契约 ${m[1]} 的 query（问题）是空的。`],
  [/^contract (\S+): expected needs one or more of .* \(unknown: (.*)\)$/, (m) => `契约 ${m[1]} 的 expected 至少要有一项检查：mustMention、mustMentionAnyOf、mustNotMention、requiredTools、forbiddenTools、shouldRefuse、shouldEscalate${m[2] !== 'none' ? `（不认识：${m[2]}）` : ''}。`],
  [/^contract (\S+): expected\.(\w+) must be (a list of strings|a list of groups|true or false).*?, not (.*)$/, (m) => `契约 ${m[1]} 的 expected.${m[2]} 要写成${{ 'a list of strings': '字符串列表，例：["15"]', 'a list of groups': '分组的列表，每组一个字符串列表，例：[["15", "十五"]]', 'true or false': ' true 或 false' }[m[3]]}，现在是 ${m[4]}。`],
  [/^no contracts$/, '至少要有一条契约。'],
  [/^none of the pack's (\d+) practice question\(s\) gives L1 anything to check/, (m) => `这个项目的 ${m[1]} 道练习题都没有 L1 能检查的项（全靠目标裁判判断），导入不出契约：在下面「手写」。`],
  // Skill Lab
  [/^skill name: lowercase letters, digits and hyphens \(at most (\d+)\)/, (m) => `技能名只用小写字母、数字和连字符（最多 ${m[1]} 个字符，例：reply-style）。`],
  [/^SKILL\.md must start with YAML frontmatter|^SKILL\.md has no frontmatter/, 'SKILL.md 开头要有 frontmatter：第一行 ---，接着 name: 和 description: 各一行，再一行 ---。'],
  [/^SKILL\.md frontmatter is not a mapping/, 'SKILL.md 开头的 frontmatter 要写成「名字: 值」：name: …、description: …。'],
  [/frontmatter needs a description/, 'SKILL.md 的 frontmatter 要写 description：模型靠它决定什么时候加载这个技能。'],
  [/^SKILL\.md has no instructions after the frontmatter/, 'SKILL.md 在 frontmatter 后面还要写正文：告诉 Agent 怎么做。'],
  [/^SKILL\.md: at most (\d+) KB/, (m) => `SKILL.md 最大 ${m[1]} KB。`],
  [/^(train|val|test|tasks)\[(\d+)\] \(([^)]*)\): (question|rubric) is required/, (m) => `${{ train: 'train 组', val: 'val 组', test: 'test 组', tasks: '' }[m[1]]}第 ${Number(m[2]) + 1} 题（${m[3]}）缺 ${m[4] === 'rubric' ? 'rubric（评分标准：怎样的回答算过）' : 'question（问题）'}，最多 4000 字。`],
  [/^task ids must be unique across the set/, '题目 id 在整个任务集里不能重复。'],
  [/^training needs train and val tasks/, '训练要有 train 和 val 两组题（只给一个列表的话至少 4 道题，按 4:3:3 分）。'],
  [/^uri: s3:\/\/bucket\/prefix\//, 'S3 位置要写成 s3://桶/前缀/（放 SKILL.md 的文件夹），或 s3://桶/前缀/SKILL.md。'],
  [/^no skills in (\S+)'s release pack/, (m) => `工作坊项目 ${m[1].replace(/'s$/, '')} 的发布包里没有技能：先在工作坊页生成并构建它。`],
  [/the best version did not beat the seed on the held-out split/, '最佳版本在留出的 test 题上没有超过初始版本：确实要发布就勾选「仍然发布」。'],
  [/^the training kept the seed/, '训练最后留下的是初始版本：没有新版本可以发布。'],
  // names, sizes and fields of the other forms
  [/^name: letters, digits, '-' or '_' \(at most (\d+)\)/, (m) => `名称只用字母、数字、- 或 _（最多 ${m[1]} 个字符，例：hr-policies）。`],
  [/^name: a letter or digit, then letters, digits, '_', '-', '\.' or '\/' \(at most (\d+)\)/, (m) => `名称要字母或数字开头，只用字母、数字和 _ - . /（最多 ${m[1]} 个字符）。`],
  [/^name: 1-80 characters/, '先填名称（1–80 个字）。'],
  [/^name is required/, '先填名称。'],
  [/^source\.bucket: an S3 bucket name/, '「Bucket」要填一个 S3 桶名。'],
  [/^upload at most (\d+) MB at a time/, (m) => `一次最多上传 ${m[1]} MB：分几次上传。`],
  [/^instructions: say what to judge and how \(at least (\d+) characters\)/, (m) => `判断说明至少 ${m[1]} 个字：写清楚判断什么、怎么判。`],
  [/^systemPrompt is required/, '系统 Prompt 不能是空的（最多 100 000 字）。'],
  [/^model(?: must be|:) a Bedrock model or inference profile id/, '模型要填 Bedrock 的模型 ID 或推理配置 ID（例：us.anthropic.claude-haiku-4-5-20251001-v1:0）。'],
  [/^each gateway needs its ARN and a tool name/, '每行 Gateway 要写「Gateway ARN 空格 工具名」，工具名只用字母和数字。'],
  [/^each skill is an s3:\/\/bucket\/prefix\//, '每行技能要写成 s3://桶/前缀/（放 SKILL.md 的文件夹）。'],
  [/^message: 1-20000 characters/, '消息要 1–20000 个字。'],
  [/^names starting with harness_ belong to the runtimes of Harnesses/, '名称不能以 harness_ 开头：那是 Harness 自己的 Runtime 用的。'],
  [/^entryPoint: the \.py file to run/, '入口文件要写 zip 根目录下的 .py 文件（例：main.py）。'],
  [/^imageUri: /, '镜像 URI 要写成 <账号>.dkr.ecr.<区域>.amazonaws.com/<仓库>:<标签>（或 @sha256:<摘要>）。'],
  [/^environment: an object of NAME: value/, '环境变量一行一个 KEY=VALUE。'],
  [/^environment: at most (\d+) variables/, (m) => `环境变量最多 ${m[1]} 个。`],
  [/^the archive is empty|^the zip holds no files/, 'zip 里没有文件。'],
  [/^the archive is not a zip/, '上传的文件不是 zip。'],
  [/^a Dockerfile build context needs the Dockerfile at the zip's root/, 'Dockerfile 构建目录 zip 的根目录要有 Dockerfile。'],
  [/^lifecycle: idleRuntimeSessionTimeout must not exceed maxLifetime/, '空闲超时不能比最长存活还长。'],
  [/^url: the MCP server's http\(s\) endpoint/, '地址要写 MCP 服务的 http(s) 地址（例：https://example.com/mcp）。'],
  [/^card: an A2A agent card/, '名片要是一张 A2A 名片（一个 JSON 对象）。'],
  [/^statement: Cedar needs a permit\(…\) or forbid\(…\) policy/, 'Cedar 策略要写 permit(…) 或 forbid(…)。'],
  [/^priority: a whole number 1-1000000/, '优先级要是 1 到 1000000 的整数。'],
  [/^text: say in 1-2000 characters who may or may not do what/, '用 1–2000 个字说清楚谁可以、谁不可以做什么。'],
  [/^only (agents|bundles|judges|knowledge bases|online evaluations|recommendations|records|registries|runtimes) (?:created|started|deployed) from this console(?: \(tag adlc:console=1\))? can be (changed or )?deleted here/,
    (m) => `只有这个控制台建的${{ agents: ' Agent ', bundles: '配置包', judges: '裁判', 'knowledge bases': '知识库', 'online evaluations': '在线评估', recommendations: '优化建议', records: '记录', registries: ' Registry ', runtimes: ' Runtime ' }[m[1]]}能在这里${m[2] ? '修改或' : ''}删除：别处建的，请在建它的地方处理。`],
  [/^no job (\S+)/, (m) => `找不到任务 ${m[1]}（也许已经清理掉了）。`],
  [/^a runtime named (\S+) exists(?: \(([^)]*)\))?: publish a new version of it instead/, (m) => `已经有一个叫 ${m[1]} 的 Runtime${m[2] ? `（${m[2]}）` : ''}：换一个名称，或者在「已部署」里打开它，发布新版本。`],
  [/^CodeBuild (\w+): (.*)$/s, (m) => {
    const how = { FAILED: '失败了', FAULT: '出了内部故障', TIMED_OUT: '超时了', STOPPED: '被停止了' }[m[1]] || `没成功（${m[1]}）`
    const log = '看任务日志里 | 开头的几行（CodeBuild 构建输出的结尾）'
    const phase = /^([A-Z_]+): (.*)$/s.exec(m[2])
    // a build that timed out before it started waited in the queue: the account's concurrent builds, not the build
    if (m[1] === 'TIMED_OUT' && phase && ['QUEUED', 'SUBMITTED'].includes(phase[1])) {
      return 'CodeBuild 构建在排队里等满了 30 分钟还没开始：多半是账号里这类 CodeBuild 构建的并发配额用完了（别的构建在跑，或新账号配额很低）。等别的构建结束再部署，或在 Service Quotas 里提高 CodeBuild 的并发构建数。'
    }
    const timedOut = m[1] === 'TIMED_OUT' ? '构建跑满了 CodeBuild 的 30 分钟上限：多半是镜像或依赖太大（例如 torch），精简 Dockerfile，或先构建好镜像、选「已有的 ECR 镜像」部署' : ''
    if (m[2] === 'see the build log') return `CodeBuild 构建${how}：${timedOut ? `${timedOut}；` : ''}${log}。`
    const hint = timedOut || (phase && ['FAILED', 'FAULT'].includes(m[1]) && PHASE_HINT[phase[1]])  // a stopped build's phase says nothing of a cause
    const said = (phase ? phase[2] : m[2]).replace(/[\s.。]+$/, '')  // a phase that said nothing comes as its own status: no detail
    return `CodeBuild ${phase ? `在「${PHASE_NAME[phase[1]] || phase[1]}」阶段` : ''}${how}${/^[A-Z_]+$/.test(said) ? '' : `：${said}`}。${log}${hint ? `；${hint}` : ''}。`
  }],
  [/^the image (\S+) has (.*?) finding\(s\) at a blocking severity \(([^)]*)\)/, (m) => `镜像 ${m[1]} 有达到拦截级别（${m[3]}）的漏洞：${m[2]}。修掉再部署，或者在「运行时选项」里把镜像扫描的门槛放宽。`],
  [/^the runtime is (\w+): (.*)$/s, (m) => `Runtime ${statusText(m[1])}：${m[2] === 'no reason given' ? 'AWS 没给原因' : m[2]}`],
  [/^the runtime is still (\w+) after (\d+) min/, (m) => `等了 ${m[2]} 分钟，Runtime 还是「${statusText(m[1])}」：过一会儿刷新看看，或者到 AgentCore 控制台看它。`],
  [/^(\S+) is (CREATING|UPDATING|DELETING): wait until it settles/, (m) => `${m[1]} 还在${statusText(m[2])}：等它完成再试。`],
  [/^network: (a subnet|a security group) does not exist in (\d+) \/ (\S+) \((.*)\)$/s, (m) => `网络设置：填的${m[1] === 'a subnet' ? '子网' : '安全组'}有的在账号 ${m[2]} / ${m[3]} 里不存在（${m[4]}）。`],
  [/^network: this workspace may not read (.+?) \((\w+)\)/, (m) => `网络设置：这个工作区没有权限读${/subnet/.test(m[1]) ? '子网' : /security group/.test(m[1]) ? '安全组' : m[1]}（${m[2]}）：检查 VPC 配置要 ec2:DescribeSubnets 和 ec2:DescribeSecurityGroups 权限，请账号管理员加上。`],
  [/^network: (.*) not found in (\d+) \/ (\S+)$/, (m) => `网络设置：在账号 ${m[2]} / ${m[3]} 里找不到 ${m[1]}。`],
  [/^network: the subnets are in several VPCs \(([^)]*)\)/, (m) => `网络设置：这些子网分属几个 VPC（${m[1]}）：一个 Runtime 只能用同一个 VPC 的子网。`],
  [/^network: the security groups (.*) are not in the subnets' VPC (\S+)$/, (m) => `网络设置：安全组 ${m[1]} 不在子网所在的 VPC ${m[2]} 里。`],
  [/^network: AgentCore Runtime's VPC mode supports (.*) in (\S+), not (.*)$/s, (m) => `网络设置：AgentCore Runtime 的 VPC 模式在 ${m[2]} 只支持可用区 ${m[1]}，这些子网不在其中：${m[3]}。`],
  [/^network\.(subnets|securityGroups): 1 to 16/, (m) => `网络设置：${m[1] === 'subnets' ? '子网' : '安全组'}要填 1 到 16 个 ID。`],
  [/^network\.(subnets|securityGroups): '([^']*)' is not a/, (m) => `网络设置：「${m[2]}」不是${m[1] === 'subnets' ? '子网 ID（subnet-…）' : '安全组 ID（sg-…）'}。`],
  [/^the image is in (\d+) \/ (\S+): this workspace deploys images of (\d+) \/ (\S+) only/, (m) => `镜像在账号 ${m[1]} / ${m[2]}：这个工作区只能部署账号 ${m[3]} / ${m[4]} 的镜像。`],
  [/^no image (\S+) in ECR/, (m) => `ECR 里没有镜像 ${m[1]}：检查仓库名和标签（或摘要）。`],
  [/^the console bucket (\S+) is not usable from this account \((.*)\)$/s, (m) => `控制台的 S3 桶 ${m[1]} 这个账号用不了（${m[2]}）：也许这个工作区没有它的权限。`],
  [/^the build pushed no (\S+)/, (m) => `构建跑完了，ECR 里却没有 ${m[1]}：看 CodeBuild 的构建日志。`],
  [/^a CodeBuild project (\S+) exists that the console did not create/, (m) => `账号里已经有一个叫 ${m[1]} 的 CodeBuild 项目，不是这个控制台建的：控制台不动它，请账号管理员改名或删掉它。`],
  [/^the ECR repository (\S+) exists and is not this runtime's/, (m) => `ECR 仓库 ${m[1]} 已经存在，但不是控制台为这个 Runtime 建的：换一个名称。`],
  [/^CodeBuild still (\w+) after (\d+) min \(([^)]*)\)/, (m) => `CodeBuild 跑了 ${m[2]} 分钟还没完成（停在「${BUILD_PHASE[m[1]] || m[1]}」，${m[3]}）：到 CodeBuild 控制台看它的日志。`],
  [/^the role (\S+) exists and is not the console's for (\S+)/, (m) => `IAM 角色 ${m[1]} 已经存在，但不是控制台为 ${m[2]} 建的：换一个名称。`],
  [/^the role (\S+) is the execution role of (\S+) in (.*?) \(its trust names that region\): .* in (\S+) needs a name of its own/s, (m) => `IAM 角色 ${m[1]} 是 ${m[2]} 在 ${m[3]} 的执行角色（IAM 名字整个账号通用）：在 ${m[4]} 部署同名的 Runtime 要换一个名称。`],
  [/^the endpoint (\S+) is (\w+): (.*)$/s, (m) => `端点 ${m[1]} ${statusText(m[2])}：${m[3] === 'no reason given' ? 'AWS 没给原因' : m[3]}`],
  [/^the endpoint (\S+) still serves (\S+) \((\w+)\) after (\d+) min/, (m) => `等了 ${m[4]} 分钟，端点 ${m[1]} 还${m[2] === 'nothing' ? '没指向任何版本' : `指向版本 ${m[2]}`}（${statusText(m[3])}）：过一会儿刷新看看。`],
  [/^the endpoints (.*) are still there after (\d+) min/, (m) => `等了 ${m[2]} 分钟，端点 ${m[1]} 还没删掉：过一会儿再删一次。`],
  [/^(\S+) has a (deploy|undeploy) job running \((\S+)\)/, (m) => `${m[1]} 有一个${m[2] === 'deploy' ? '部署' : '下线'}任务在跑（${m[3]}）：等它结束。`],
  [/^(.+?): the runtime canary (\S+) of (.*?) is (\w+), and its job is publishing production/, (m) => `${m[1]}：${m[3]} 的金丝雀 ${m[2]} 正在发布生产版本、移动端点：等它结束。`],
  [/^(.+?) belongs to the runtime canary (\S+) \((\w+)\) of (.*?): its gate/, (m) => `${m[1]} 属于 ${m[4]} 的金丝雀 ${m[2]}（${m[3]}）：它的门禁针对的就是这几个版本。先在「A/B 与发布 → 代码 Agent 金丝雀」回滚或发布它，再清理它。`],
  [/^not a runtime id: (.*)$/, (m) => `${m[1]} 不是 Runtime ID。`],
  [/^only a named endpoint can be deleted/, '只能删命名端点：DEFAULT 端点随 Runtime 一起删。'],
  [/^version: a version number of the runtime/, '版本要填这个 Runtime 的版本号。'],
  [/^the archive is ([\d.]+ [KM]B): at most ([\d.]+ [KM]B)/, (m) => `zip 有 ${m[1]}，最多 ${m[2]}：大的依赖改用 Dockerfile 构建。`],
  [/^the zip holds more than (\d+) files/, (m) => `zip 里的文件超过 ${m[1]} 个。`],
  [/^unsafe path in the zip: (.*)$/, (m) => `zip 里有不安全的路径：${m[1]}（不能有 .. 或绝对路径）。`],
  [/^the zip holds a symbolic link: (.*)$/, (m) => `zip 里有符号链接：${m[1]}（不支持）。`],
  [/^the zip unpacks to more than (.*)$/, (m) => `zip 解压后超过 ${m[1]}。`],
  [/^the entry point (\S+) is not in the zip \(at its root: (.*)\)$/, (m) => `zip 里没有入口文件 ${m[1]}（根目录下有：${m[2] === 'nothing' ? '没有文件' : m[2]}）：改「入口文件」，或者把它放到 zip 的根目录。`],
  [/^environment: '(.*)' is not a variable name/, (m) => `环境变量名「${m[1]}」不合法：只用字母、数字和下划线。`],
  [/^environment: (\S+) is longer than 5000 characters/, (m) => `环境变量 ${m[1]} 的值超过 5000 个字。`],
  [/^lifecycle\.(\w+): (\d+) to (\d+) seconds/, (m) => `${m[1] === 'maxLifetime' ? '最长存活' : '空闲超时'}要在 ${Math.round(m[2] / 60)} 分钟到 ${Math.round(m[3] / 3600)} 小时之间。`],
  [/^lifecycle\.(\w+): whole seconds/, (m) => `${m[1] === 'maxLifetime' ? '最长存活' : '空闲超时'}要填整数。`],
  [/^sessionStorage\.mountPath: one directory under \/mnt/, '会话存储的挂载路径要是 /mnt 下的一级目录（例：/mnt/workspace）。'],
  [/^knowledgeBases: up to (\d+) knowledge base ids/, (m) => `最多挂 ${m[1]} 个知识库（每个 ID 10 个字母或数字）。`],
  [/^the smoke call answered (\d+): (.*)$/s, (m) => `冒烟调用返回了 ${m[1]}：${m[2]}（Runtime 已部署；原因多半在它的日志里）`],
  // the other pages' refusals: what is busy, not ready, someone else's, or out of range, and what to do
  [/^(\S+) is still changing: delete it again in a minute/, (m) => `${m[1]} 还在变化中：过一分钟再删。`],
  [/^give systemPrompt and\/or model/, '至少改一项：系统 Prompt 或模型。'],
  [/^this workspace may not change the role (\S+) \(([^)]*)\): a spoke role changes only roles tagged/, (m) => `这个工作区不能改角色 ${m[1]}（${m[2]}）：spoke 角色只能改带 adlc:console=1 标签、带控制台权限边界的角色。请账号管理员给它打上标签和边界，或者按原文里的内联策略手动加上。`],
  [/^this agent is in an A\/B test \(([^)]*)\): promote, stop or clean it up/, (m) => `这个 Agent 正在 A/B 实验（${m[1]}）里：先在「A/B 与发布」页发布、停止或清理它，再直接修改。`],
  [/^this Harness is the treatment of the A\/B test (\S+)/, (m) => `这个 Harness 是 A/B 实验 ${m[1]} 的候选：要改它，在「A/B 与发布 → A/B 实验」用新的配置包版本另起实验，不在这里改。`],
  [/needs an admin/, '这一步要管理员来做：请管理员操作，或换管理员账号登录。'],
  [/^(.+?) is (\w+): wait until it is READY/, (m) => `${subject(m[1])} 还是「${stateText(m[2])}」：等它就绪再试。`],
  [/^A\/B test (\S+) is still (RUNNING|PAUSED|STOPPED) after ([\d.]+) s$/, (m) => `等了 ${secs(m[3])}，A/B 实验 ${m[1]} 的运行状态还是「${statusText(m[2])}」，没切换过去：到 AgentCore 控制台看看它，再操作一次。`],
  [/^(.+?) (?:is|was) still (\w+) after ([\d.]+) s$/, (m) => `等了 ${secs(m[3])}，${subject(m[1])} 还是「${stateText(m[2])}」${WAIT_TAIL}`],
  [/^A\/B test (\S+) is stopped/, (m) => `A/B 实验 ${m[1]} 已经停了（到了最长运行时间，或在 AgentCore 控制台被停掉）：不能再改分流；看结果，或清理这个实验。`],
  [/^(?:the )?((?:A\/B test|endpoint|Gateway target|Gateway|Harness|target|online evaluation|memory)(?: \S+)?) is ([A-Z_]+)$/, (m) => `${subject(m[1])} 的状态是「${statusText(m[2])}」。`],
  [/^Gateway target (\S+): (\w+) ?(.*)$/s, (m) => `Gateway 目标 ${m[1]} ${statusText(m[2])}${m[3].trim() ? `：${m[3].trim()}` : ''}。`],
  [/^(Gateway target|Gateway) (\S+) was not READY after 5 minutes/, (m) => `等了 5 分钟，${m[1] === 'Gateway target' ? 'Gateway 目标' : 'Gateway'} ${m[2]} 还没就绪：过一会儿再试。`],
  // the assistant
  [/^the assistant is still answering in this conversation/, '助手还在回答这段对话：等它答完。'],
  [/^an approval of this conversation is still running/, '这段对话的批准还在执行：等它结束。'],
  [/^the assistant is answering: (edit|approve) after its reply/, (m) => `助手正在回答：等它答完再${m[1] === 'edit' ? '编辑' : '批准'}。`],
  [/^this conversation reached (\d+) (revisions|turns): start a new one/, (m) => `这段对话已经有 ${m[1]} ${m[2] === 'turns' ? '轮' : '版提案'}了：新开一段对话。`],
  [/^this conversation has no revision (\S+)/, (m) => `这段对话没有第 ${m[1]} 版。`],
  [/^revision (\d+) is the latest now: (edit that one|review it)/, (m) => `第 ${m[1]} 版现在是最新的：${m[2] === 'review it' ? '先看它' : '编辑那一版'}。`],
  [/^turn (\d+) of this conversation is still running/, (m) => `这段对话的第 ${m[1]} 轮还在进行：等它结束。`],
  [/^message is empty/, '消息是空的。'], [/^message: at most (\d+) characters/, (m) => `消息最多 ${m[1]} 个字。`],
  [/^revision (\d+) is not what you reviewed/, (m) => `第 ${m[1]} 版和你看过的不一样（内容变了）：再看一遍。`],
  [/^the approval of revision (\d+) is still running/, (m) => `第 ${m[1]} 版的批准还在执行：等它结束。`],
  [/^revision (\d+) no longer validates in this workspace/, (m) => `第 ${m[1]} 版在这个工作区里已经校验不过了：让助手改一版，或手动编辑。`],
  [/^what revision (\d+) refers to changed after it was reviewed/, (m) => `第 ${m[1]} 版引用的东西在审阅后变了：让助手重新提一版，或编辑出新的一版。`],
  [/^revision (\d+) is (\w+): only the latest valid draft can be approved/, (m) => `第 ${m[1]} 版是「${{ invalid: '校验没过', superseded: '已被新版取代', approved: '已批准' }[m[2]] || m[2]}」：只能批准最新的、校验通过的草稿。`],
  [/^the conversation changed meanwhile/, '对话在这期间变了：看最新的一版。'],
  [/^no conversation (\S+)/, (m) => `没有对话 ${m[1]}（也许已经删了）。`],
  [/^no skill (\S+) in (?:the|this workspace's) library/, (m) => `技能库里没有 ${m[1]}。`],
  [/^(\S+) has no version (\S+)$/, (m) => `${m[1]} 没有版本 ${m[2]}。`],
  // keys, samples
  [/^no key (\S+)/, (m) => `没有密钥 ${m[1]}（也许已经吊销）。`], [/^no such sample brief/, '没有这个样例简报。'],
  // evaluation
  [/^no contract set (\S+)/, (m) => `没有契约集 ${m[1]}（也许已经删了）。`],
  [/^the contract set id (\S+) belongs to another workspace/, (m) => `契约集 ${m[1]} 属于另一个工作区。`],
  [/^workshop project (\S+) has no built release/, (m) => `工作坊项目 ${m[1]} 还没有构建：先在工作坊页构建它。`],
  [/^verification runs on a Harness/, '验证只能对 Harness 跑（Runtime 的请求格式由它自己的代码决定）。'],
  [/^repeat: 1-5 rounds/, '轮数要 1–5。'],
  [/^plan from a finished verification that ran the evaluator panel/, '先要有一次跑完、带评估器对照的验证，才能据此生成在线评估方案。'],
  [/^scale: at least two levels/, '评分刻度至少要两档。'],
  [/^the agent has no runtime to read sessions from/, '这个 Agent 没有 Runtime，读不到它的会话。'],
  [/^(\S+) has no runtime to read traces from/, (m) => `${m[1]} 没有 Runtime，读不到它的调用记录。`],
  [/^the prompt is too long to translate in one piece/, '提示词太长，没法一次翻译给 AgentCore 分析：缩短它，或者不翻译直接分析。'],
  [/^no such recommendation/, '没有这条优化建议（也许已经删了）。'],
  [/^the recommendation is (\w+)(?:: (.*?))?: nothing to adopt yet/s, (m) => `这条优化建议是「${statusText(m[1])}」${m[2] ? `（${m[2]}）` : ''}：还没有可以采用的内容。`],
  [/^adopting needs a Harness started from this console/, '只有这个控制台建的 Harness 能采用优化建议（它的提示词放在配置包里）。'],
  [/^(\S+) has no system prompt the console can read/, (m) => `读不到 ${m[1]} 的系统 Prompt：把它填进「系统 Prompt」。`],
  [/^(\S+) has no tool descriptions the console can read/, (m) => `读不到 ${m[1]} 的工具描述（只读得到 Lambda 目标的 Gateway 工具）。`],
  [/^evaluators: 1 to (\d+)/, (m) => `评估器要 1–${m[1]} 个。`], [/^evaluators: 1-(\d+) evaluator ids/, (m) => `评估器要 1–${m[1]} 个。`],
  [/^onlineEvaluationConfigArn: /, '先选一个在线评估。'], [/^batchEvaluationArn: /, '先选一个批量评估。'],
  [/^AgentCore refuses a batch evaluation as the traces of a tool-description recommendation/, 'AgentCore 不接受用批量评估的记录做工具描述的优化建议：换成在线评估或日志。'],
  [/^no Harness named '?([^']+?)'? in this account and region/, (m) => `这个账号和区域里没有叫 ${m[1]} 的 Harness。`],
  [/^no records for this session in the window/, '这个时间窗里没有这个会话的记录：调用记录在对话后几分钟才到，过一会儿再看，或把时间窗放大。'],
  // A/B tests and runtime canaries
  [/^agentId: the Harness's id/, '先选一个 Harness。'], [/^bundleId and versionId: /, '先选配置包和版本。'],
  [/^(.+?) was not created from this console \(agent code reading it would run the new version\)/, (m) => `${m[1]} 不是这个控制台建的（读它的 Agent 代码会跑新版本）：要给它加版本得先确认。`],
  [/^nothing changed: give a different systemPrompt or model/, '没有改动：换一个不同的系统 Prompt 或模型。'],
  [/^experiment (\S+) uses this bundle: clean it up first/, (m) => `实验 ${m[1]} 在用这个配置包：先清理那个实验。`],
  [/^bundle (\S+) version (\S+) has no configuration for (\S+)/, (m) => `配置包 ${m[1]} 的版本 ${m[2]} 里没有 ${m[3]} 的配置。`],
  [/^bundle (\S+) version (\S+) sets no systemPrompt/, (m) => `配置包 ${m[1]} 的版本 ${m[2]} 没有设系统 Prompt。`],
  [/^this treatment is the agent's current configuration/, '这个候选就是 Agent 现在的配置：直接在「评估」页验证 Agent 本身。'],
  [/^the treatment version is the agent's current configuration/, '候选版本和 Agent 现在的配置一样：没有可比的。'],
  [/^no experiment (\S+)/, (m) => `没有实验 ${m[1]}（也许已经清理掉了）。`], [/^no runtime canary (\S+)/, (m) => `没有金丝雀 ${m[1]}（也许已经清理掉了）。`],
  [/^treatmentWeight: 1-99 % \(100 % is the promotion\)/, '候选的流量要 1–99%（100% 就是发布：用门禁卡的「发布」按钮）。'], [/^treatmentWeight: the treatment's share/, '候选的流量要 1–99%。'],
  [/^treatmentWeight: at most (\d+) % to start/, (m) => `开始时候选最多分 ${m[1]}% 的流量：门槛通过后才能加大。`],
  [/^metric must be one of the experiment's evaluators \(([^)]*)\)/, (m) => `判定指标要是这个实验的评估器之一（${m[1]}）。`],
  [/^(\S+) says how often the agent refused, not how well it answered/, (m) => `${m[1]} 只说明 Agent 拒绝得多不多，不说明答得好不好：判定指标换一个质量类的评估器。`],
  [/^(\S+) no longer runs this bundle's control version/, (m) => `${m[1]} 跑的已经不是这个配置包的对照版本（提示词或模型改过）：用它现在的配置新建一个配置包。`],
  [/^(?:experiment|canary) (\S+) is (\w+): only a running or paused A\/B test takes a new split/, (m) => `${m[1]} 是「${stateText(m[2])}」：只有运行中或暂停的 A/B 实验能改分流。`],
  [/^(?:experiment|canary) (\S+) is (\w+): traffic goes to a running A\/B test/, (m) => `${m[1]} 是「${stateText(m[2])}」：只有运行中的 A/B 实验接流量。`],
  [/^(?:experiment|canary) (\S+) is (\w+)(?: now)?: nothing to (promote|roll back)/, (m) => `${m[1]} 是「${stateText(m[2])}」：没有可以${m[3] === 'promote' ? '发布' : '回滚'}的。`],
  [/^canary (\S+) is (\w+): a candidate runtime is verified while its canary runs/, (m) => `金丝雀 ${m[1]} 是「${stateText(m[2])}」：候选 Runtime 只在金丝雀运行、暂停或停止时验证。`],
  [/^canary (\S+) is (\w+): wait for its job/, (m) => `金丝雀 ${m[1]} 是「${stateText(m[2])}」：等它的任务结束。`],
  [/^(?:experiment|canary) (\S+) is still being set up/, (m) => `${m[1]} 还在创建：等它的任务结束。`],
  [/^(?:experiment|canary) (\S+) is being cleaned up/, (m) => `${m[1]} 正在清理：等它的任务结束。`],
  [/^(?:experiment|canary) (\S+) is already cleaned up/, (m) => `${m[1]} 已经清理过了。`],
  [/^(?:experiment|canary) (\S+) is (\w+)$/, (m) => `${m[1]} 是「${stateText(m[2])}」。`],
  [/^canary (\S+) has no candidate runtime yet/, (m) => `金丝雀 ${m[1]} 还没有候选 Runtime：等它建好。`],
  [/^give a contractSet or prompts, at most (\d+) sessions/, (m) => `先选一个契约集或写几条问题（总共最多 ${m[1]} 个会话）。`],
  [/^the agent already runs the treatment/, 'Agent 已经在跑候选版本了。'],
  [/^cleanup left (\d+) resource\(s\) \((.*)\): clean up again$/s, (m) => `清理留下了 ${m[1]} 个资源（${m[2].split(', ').map(resourceText).join('，')}）：看「清理结果」里的原因，处理好后再点「清理」。`],
  [/^an override of the gate needs a reason/, '门禁没通过时覆盖发布要写原因（至少 5 个字）：为什么不按证据来。'],
  [/^systemPrompt: 1-100 000 characters/, '系统 Prompt 要 1–100 000 字。'],
  [/^the agent's model is not a Bedrock model configuration/, '这个 Agent 的模型不是 Bedrock 模型配置：候选只能改提示词。'],
  [/^(\S+) already has an experiment running/, (m) => `${m[1]} 已经有一个实验在跑：先停止并清理它。`],
  [/^(\S+) already has a canary/, (m) => `${m[1]} 已经有一个金丝雀：先回滚或发布它，再清理它。`],
  [/^(\S+) changed while the experiment was being set up/, (m) => `${m[1]} 在建实验的过程中被改了：重新建一次。`],
  [/^(\S+) moved to version (\S+) while the canary was being set up/, (m) => `${m[1]} 在建金丝雀的过程中变成了版本 ${m[2]}：重新建一次。`],
  [/^the (treatment|candidate) is worse than (the control|production) beyond the noise band/, (m) => `候选比${m[2] === 'production' ? '生产' : '对照'}差，超出了噪声带（数字见门禁卡的 A/B 一条）：要加大它的流量得先确认。`],
  [/^(\S+) speaks (\w+): a canary splits HTTP agents/, (m) => `${m[1]} 用 ${m[2]} 协议：金丝雀只能分流 HTTP 协议的 Agent。`],
  [/^(\S+) takes JWT bearer tokens/, (m) => `${m[1]} 用 JWT 认证：金丝雀的 Gateway 只能用 IAM（SigV4）调用它。`],
  [/^(\S+) runs code from a zip: the candidate is a zip too/, (m) => `${m[1]} 跑的是 zip 代码：候选也要是 zip。`],
  [/^(\S+) runs a container image: the candidate is a Dockerfile build or an image/, (m) => `${m[1]} 跑的是容器镜像：候选要是 Dockerfile 构建或镜像。`],
  [/^the candidate (\S+) is version (\S+) now, not version (\S+) the canary measures/, (m) => `候选 ${m[1]} 现在是版本 ${m[2]}，不是金丝雀在比的版本 ${m[3]}：验证它说明不了这次金丝雀，另起一个金丝雀。`],
  [/^the canary (\S+) of (\S+) is (\w+), and its job is moving production now/, (m) => `${m[2]} 的金丝雀 ${m[1]} 是「${stateText(m[3])}」，它的任务正在切换生产版本：等它结束。`],
  [/^(\S+): production moves under one job at a time/, (m) => `${m[1]}：生产版本一次只能由一个任务切换：等前一个结束。`],
  [/^the candidate runs with production's role, which pulls from (\S+)/, (m) => `候选用生产的角色运行，它只能从 ${m[1]} 拉镜像：把候选镜像推到那里，或者给它的 Dockerfile。`],
  [/^the candidate is production's own image and variables/, '候选和生产用的是同一个镜像和环境变量：没有可比的。'],
  [/^(\S+) has no console image repository (\S+)/, (m) => `${m[1]} 没有控制台的镜像仓库 ${m[2]}：Dockerfile 候选要在生产镜像所在的仓库里构建。`],
  [/^candidate: no image (\S+) in ECR/, (m) => `ECR 里没有候选镜像 ${m[1]}：检查仓库名和标签。`],
  [/^an experiment creates a treatment Harness, a Gateway, two online evaluations/, '实验会建一个候选副本 Harness、一个 Gateway、两个在线评估（按评分的会话计费）和一个 A/B 测试，清理前一直保留：先勾选确认会产生这些资源和费用。'],
  [/^a canary deploys the candidate as a runtime of its own and creates a Gateway/, '金丝雀会把候选部署成单独的 Runtime，并建一个 Gateway、两个在线评估（按评分的会话计费）、一个 A/B 测试和生产上的一个端点，清理前一直保留：先勾选确认会产生这些资源和费用。'],
  [/^candidate: the new version, as the deploy page sends it/, '候选要按「部署代码」页的格式给：来源（zip、压缩包或 imageUri）和环境变量。'],
  [/^candidate: (.+)$/s, (m) => { const t = consoleText(m[1]); return t && `候选的部署参数不对：${t}` }],
  [/^another action on (?:the runtime )?create:(\S+) is still running: try again in a minute/, (m) => `同名的 Runtime ${m[1]} 正在创建（另一次部署刚开始）：等一分钟再试。`],
  [/^another action on (?:the agent |the runtime )?(\S+) is still running: try again in a minute/, (m) => `${m[1]} 上还有另一个操作在进行（部署、发布或回滚）：等一分钟再试。`],
  [/^the Gateway's role may not use this policy engine( \(the role was not created from this console)?/, (m) => `Gateway 的角色没有权限用这个策略引擎：${m[1] ? '这个角色不是控制台建的，把卡片里的权限语句自己加到它上面' : '点下面的「给角色授权并挂上」'}${m[1] ? '，再挂一次' : ''}。`],
  [/^(\S+) ignored the call's (skills|input files) \(its image is from an older template\)/, (m) => `${m[1]} 没理会这次调用带的${m[2] === 'skills' ? ' Skill' : '输入文件'}（它的镜像是旧版模板做的）：到「部署代码」部署它的新版本，再评估一次。`],
  [/^(\d+) of these tasks give the agent input files \(([^)]*)\), and a Harness answers in a sandbox/, (m) => `这些题里有 ${m[1]} 道要给 Agent 输入文件（${m[2]}），Harness 的沙箱放不进文件：换成 Claude Agent SDK 的 Runtime 来评估（部署代码 → 从模板生成：Claude Agent SDK），或者只用不带文件的题。`],
  [/^(\w+)\[(\d+)\] \(([^)]*)\)(?:\.files)?: (.*)$/s, (m) => { const t = taskFileText(m[4]); return t !== m[4] && `第 ${Number(m[2]) + 1} 道题（${m[3]}）的输入文件：${t}` }],
  [/^the ECR repository (\S+) is not (\S+)'s \(tags/, (m) => `ECR 仓库 ${m[1]} 不是 ${m[2]} 的：换一个名称。`],
  // policies and the Gateway
  [/^only policy engines created from this console/, '只有这个控制台建的策略引擎（和其中的策略）能在这里修改：别处建的，请在建它的地方处理。'],
  [/^detach the engine from (.*) first/, (m) => `先把引擎从 ${m[1]} 上解绑：Gateway 会一直指向已删除的引擎。`],
  [/^the engine still holds (\d+) policies/, (m) => `引擎里还有 ${m[1]} 条策略：要连同策略一起删，先确认。`],
  [/^give a statement, an enforcementMode and\/or a description/, '至少改一项：策略、执行方式或说明。'],
  [/^Gateway (\S+) was not created from this console and every agent using it is affected/, (m) => `Gateway ${m[1]} 不是这个控制台建的，用它的每个 Agent 都会受影响：要改它得先确认。`],
  [/^the policy engine is (\w+)/, (m) => `策略引擎是「${statusText(m[1])}」：等它就绪再试。`],
  [/^the Gateway is (\w+): (.*)$/s, (m) => `Gateway ${statusText(m[1])}：${m[2]}`],
  [/^the Gateway's role has the policy permissions now, but the service still refuses/, 'Gateway 的角色已经有策略权限了，但服务还在拒绝：过一分钟再试。'],
  [/^this rule is managed by (.*): change it there/, (m) => `这条规则由 ${m[1]} 管理：到那里改。`],
  [/^policy: not JSON \((.*)\)$/s, (m) => `策略不是合法的 JSON（${m[1]}）。`], [/^policy: an IAM policy document/, '策略要是一份 IAM 策略文档：带 Statement 列表的 JSON。'],
  [/^statement: (\d+)-(\d+) characters of Cedar/, (m) => `Cedar 策略要 ${m[1]}–${m[2]} 个字符。`],
  [/^(\S+): not a valid id \((.*)\)$/, (m) => `${m[1]} 不是合法的 ID（${m[2]}）。`],
  [/^conditions: at most two/, '条件最多两条。'], [/^actions: one or two/, '动作要一到两个。'],
  [/^(matchPaths|matchPrincipals): anyOf needs at least one entry/, (m) => `${m[1] === 'matchPaths' ? '路径条件' : '调用者条件'}的 anyOf 至少要有一条。`],
  [/^matchPaths: each path is \/<target>\/\* \(got (.*)\)$/, (m) => `路径要写成 /<目标名>/*（现在是 ${m[1]}）。`],
  [/^matchPrincipals: each entry is/, '调用者条件每条要写成 {"iamPrincipal": {"arn": "<IAM 用户/角色或 assumed-role ARN>", "operator": "StringEquals 或 StringLike"}}。'],
  [/^staticRoute: targetName/, 'staticRoute 要写 targetName（目标名）。'], [/^staticOverride: bundleArn and bundleVersion/, 'staticOverride 要写 bundleArn 和 bundleVersion（配置包和它的版本）。'],
  [/^(\w+): exactly two weighted entries/, (m) => `${m[1]} 要正好两项，各带一个权重。`],
  [/^(\w+): each entry has a name, a whole weight 1-99 and its (\w+)/, (m) => `${m[1]} 的每一项要有名字、1–99 的整数权重和 ${m[2]}。`],
  [/^(\w+): one of (.*)$/, (m) => `${m[1]} 只能是这些之一：${m[2]}。`],
  [/^dimensionKeys: 1-10 different keys/, '限流维度要 1–10 个不同的键。'], [/^dimensionKeys: '(.*)' is not one of (.*)$/, (m) => `限流维度「${m[1]}」不认识：只能是 ${m[2].replace(/ or /g, ' 或 ')}。`],
  [/^entries: 1-1000/, '限流条目要 1–1000 条。'],
  [/^entry (\d+): dimensions give a value for exactly the keys (.*)$/, (m) => `第 ${Number(m[1]) + 1} 条限流：dimensions 要正好给这些键的值：${m[2]}。`],
  [/^entry (\d+): '\*' only in trailing positions/, (m) => `第 ${Number(m[1]) + 1} 条限流：* 只能放在末尾（一个 * 之后的键都要是 *）。`],
  [/^entry (\d+): (\w+) is one \{rate, period\}/, (m) => `第 ${Number(m[1]) + 1} 条限流：${m[2]} 要写成一个 {"rate": …, "period": …}。`],
  [/^entry (\d+): (\w+) rate is a number/, (m) => `第 ${Number(m[1]) + 1} 条限流：${m[2]} 的 rate 要是数字。`],
  [/^entry (\d+): (\w+) rate 0-10000000/, (m) => `第 ${Number(m[1]) + 1} 条限流：${m[2]} 的 rate 要在 0–10000000 之间。`],
  [/^entry (\d+): (\w+) are counted per (.*)$/, (m) => `第 ${Number(m[1]) + 1} 条限流：${m[2]} 只能按 ${m[3].replace(/ or /g, ' 或 ')} 计。`],
  [/^entry (\d+): limit requests, tokens and\/or connections/, (m) => `第 ${Number(m[1]) + 1} 条限流：至少限一项：requests、tokens 或 connections。`],
  // knowledge bases
  [/^every file needs a name/, '每个文件都要有名字。'],
  [/^no data source of this KB reads the console's upload folder: add the files where its source reads \((.*)\)$/s, (m) => `这个知识库没有数据源读控制台的上传目录：把文件放到它的数据源读的位置（${m[1]}）。`],
  [/^query is empty/, '问题是空的。'], [/^knowledge base (\S+) is (\w+)$/, (m) => `知识库 ${m[1]} 是「${statusText(m[2])}」。`],
  // the registry
  [/^(\S+) is (\d+) characters: the registry keeps at most (\d+)/, (m) => `${m[1]} 有 ${m[2]} 个字：Registry 最多存 ${m[3]} 个。`],
  [/^no registry (\S+): a registry id/, (m) => `没有 Registry ${m[1]}（Registry ID 是 12–16 个字母和数字）。`], [/^no record (\S+): a record id/, (m) => `没有记录 ${m[1]}（记录 ID 是 12 个字母和数字）。`],
  [/^the registry holds (\d+) record\(s\) this console did not create/, (m) => `Registry 里有 ${m[1]} 条不是这个控制台建的记录：Registry 保留，不删。`],
  [/^the registry holds (\d+) record\(s\): delete them first/, (m) => `Registry 里还有 ${m[1]} 条记录：先删掉它们，或者选连同记录一起删。`],
  [/^card: an A2A 0\.3 card needs (.*)$/, (m) => `A2A 0.3 名片要有 ${m[1]}。`], [/^card: skills is a list and capabilities an object/, '名片里 skills 要是列表，capabilities 要是对象。'],
  [/^card: skill (\S+) needs (.*?) \(tags/, (m) => `名片里的技能 ${m[1]} 要有 ${m[2]}（tags 是一组词）。`], [/^card: not JSON \((.*)\)$/s, (m) => `名片不是合法的 JSON（${m[1]}）。`],
  [/^Gateway (\S+) is not an MCP Gateway/, (m) => `Gateway ${m[1]} 不是 MCP Gateway。`],
  [/^SKILL\.md name (\S+): 1-64 lowercase letters/, (m) => `SKILL.md 的 name「${m[1]}」要 1–64 个小写字母、数字和单个连字符（Registry 不收别的）。`],
  [/^SKILL\.md frontmatter is not YAML: (.*)$/s, (m) => `SKILL.md 的 frontmatter 不是合法的 YAML：${m[1]}`],
  [/^version: letters, digits, '\.' and '-'/, '版本号只用字母、数字、. 和 -（例：1.0.0）。'],
  [/^a DEPRECATED record is terminal/, '已下线的记录不能再改。'],
  [/^nothing to change: give description, displayName, version or the content/, '没有改动：至少改一项说明、显示名、版本或内容。'],
  [/^evidence is a verification of an agent: publish an agent record/, '证据是 Agent 的验证结果：要发布 Agent 类型的记录。'],
  [/^the record has no readable agent card/, '这条记录没有能读的 Agent 名片。'],
  [/^the card names no agent/, '名片里没有指明是哪个 Agent（没有 urn:adlc:console:agent 扩展）：选一个 Agent。'],
  [/^each tool needs a name/, '每个工具都要有名字。'], [/^tools: a list of \{name, description, inputSchema\}/, '工具要写成列表：每个 {name, description, inputSchema}。'],
  [/^(s3:\/\/\S+) is not UTF-8 text/, (m) => `${m[1]} 不是 UTF-8 文本。`],
  // Skill Lab: the library, files, task sets, trainings
  [/^description: at most 1024 characters/, '说明最多 1024 个字。'],
  [/^(\S+) is used by (.*): take it off those agents first/, (m) => `${m[1]} 还在被 ${m[2]} 用：先把它从这些 Agent 上拿掉。`],
  [/^(\S+) has no execution role to grant/, (m) => `${m[1]} 没有执行角色，没法授权。`],
  [/^(\S+) is not a usable xlsx: (.*)$/s, (m) => `${m[1]} 不是能用的 xlsx（${{ "the workbook's members are missing": '缺少工作簿的内容', 'too many members': '里面的文件太多', 'it unpacks to more than 200 MiB': '解压后超过 200 MiB', 'a member is compressed more than 100 times (a zip bomb?)': '压缩比异常（也许是压缩炸弹）', 'a workbook with macros is not taken': '带宏的工作簿不收' }[m[2]] || m[2]}）。`],
  [/^(\S+): a task file is (.*)$/, (m) => `${m[1]}：任务文件只能是 ${m[2]}。`],
  [/^(\S+) is not a (\w+) file: its content is binary/, (m) => `${m[1]} 不是 ${m[2]} 文件：内容是二进制的。`],
  [/^(\S+) holds NUL bytes: not a (\w+) file/, (m) => `${m[1]} 里有 NUL 字节：不是 ${m[2]} 文件。`],
  [/^(\S+) is not UTF-8 text \((.*)\): save it as UTF-8/, (m) => `${m[1]} 不是 UTF-8 文本（${m[2]}）：另存为 UTF-8 再上传。`],
  [/^(\S+) is not a (\w+) file \(its content does not match its extension\)/, (m) => `${m[1]} 不是 ${m[2]} 文件：内容和扩展名对不上。`],
  [/^files: at most (\d+) an upload/, (m) => `一次最多上传 ${m[1]} 个文件。`], [/^files: two of them have the same name/, '有两个文件同名。'],
  [/^files\[\d+\]\.name: a plain file name/, '文件名只能是普通的名字（不带文件夹、不以 . 开头、最多 120 个字符）。'],
  [/^(\S+) is empty$/, (m) => `${m[1]} 是空的。`],
  [/^(\S+) is ([\d.]+) MiB: at most (\d+) MiB a file/, (m) => `${m[1]} 有 ${m[2]} MiB：每个文件最多 ${m[3]} MiB。`],
  [/^an upload is at most (\d+) MiB/, (m) => `一次最多上传 ${m[1]} MiB。`],
  [/^no task file sha256:(\S+) in this workspace/, (m) => `这个工作区没有任务文件 sha256:${m[1]}（也许已经删了）。`],
  [/^(.+?) is a file of the task set (.*?): delete the set or take the file off its tasks first/, (m) => `${m[1]} 是任务集 ${m[2]} 用的文件：先删掉任务集，或者把文件从它的题目上拿掉。`],
  [/^(.+?): '(.*)' is not a plain relative path/, (m) => `${m[1]}：「${m[2]}」不是普通的相对路径（最多 4 层，不能有 ..，不能以 . 开头）。`],
  [/^(\S+)\.files: sha256:(\S+) is not a file uploaded to this workspace/, (m) => `${m[1]} 的文件 sha256:${m[2]} 不是上传到这个工作区的：先上传它。`],
  [/^(\S+)\.files: (.*) must end in the file's own extension \(it is a (\w+) file\)/, (m) => `${m[1]} 的文件 ${m[2]} 的扩展名要和它的类型一致（它是 ${m[3]} 文件）。`],
  [/^(\S+)\.files: at most (\d+) MiB a task/, (m) => `${m[1]}：每道题的文件最多 ${m[2]} MiB。`], [/^(\S+)\.files: at most (\d+) a task/, (m) => `${m[1]}：每道题最多 ${m[2]} 个文件。`],
  [/^a task set names at most (\d+) files/, (m) => `一个任务集最多用 ${m[1]} 个文件。`], [/^a task set's files are at most (\d+) MiB/, (m) => `一个任务集的文件加起来最多 ${m[1]} MiB。`],
  [/^(train|val|test|tasks): at least one task/, (m) => `${m[1] === 'tasks' ? '' : `${m[1]} 组`}至少要有一道题。`],
  [/^(train|val|test|tasks): a list of at most (\d+) tasks/, (m) => `${m[1] === 'tasks' ? '题目' : `${m[1]} 组`}最多 ${m[2]} 道。`],
  [/^(train|val|test|tasks)\[(\d+)\]\.id: letters, digits/, (m) => `${m[1] === 'tasks' ? '' : `${m[1]} 组`}第 ${Number(m[2]) + 1} 题的 id 只用字母、数字、.、_ 或 -。`],
  [/^no task set (\S+) in this workspace/, (m) => `这个工作区没有任务集 ${m[1]}（也许已经删了）。`],
  [/^the task files (.*) are no longer in the console bucket/, (m) => `任务文件 ${m[1]} 已经不在控制台的桶里了：重新上传，再保存任务集。`],
  [/^(\S+) is (\w+), not READY$/, (m) => `${m[1]} 是「${statusText(m[2])}」，还没就绪：等它就绪再试。`],
  [/^(\S+) has no successful deployment to (\S+) yet/, (m) => `${m[1]} 还没有成功部署到 ${m[2]}：先部署一次。`],
  [/^the (train|val|test) split has no tasks/, (m) => `${m[1]} 组没有题。`],
  [/^not a training of this workspace/, '这不是这个工作区的训练。'], [/^the training is (\w+)$/, (m) => `这次训练是「${JOB_LABEL[m[1]] || m[1]}」。`],
  [/^no sample (\S+) \(samples: (.*)\)$/, (m) => `没有样例 ${m[1]}（有的样例：${m[2]}）。`],
  [/^the model's reply held no JSON/, '模型的回复里没有 JSON：再试一次。'],
  [/^(\S+) must be a Bedrock model or inference profile id/, (m) => `${m[1]} 要填 Bedrock 的模型 ID 或推理配置 ID。`],
  // the Studio
  [/^this workspace has (\d+) Studio projects: delete some first/, (m) => `这个工作区已经有 ${m[1]} 个 Studio 项目：先删掉一些。`],
  [/^not a project id: (.*)$/, (m) => `${m[1]} 不是项目 ID。`], [/^name: the project's name/, '先填项目名称。'], [/^version: a version number$/, '版本要填版本号。'],
  [/^minutes: a number of minutes/, '分钟数要是一个数字。'],
  // Kiro CLI under the console (the assistant's turns, the workshop's generations)
  [/^(?:Kiro CLI|kiro-cli) exited (-?\d+): ?(.*)$/s, (m) => `Kiro CLI 出错退出（退出码 ${m[1]}）：${m[2].trim() || '它没说原因'}`],
  // AWS's own refusals, last: a message that names its step keeps that step's words
  [/^not found: (.*)$/s, (m) => `要找的资源不存在（也许已经删了）：${m[1]}`],
  [/^Parameter validation failed:\s*(.*)$/s, (m) => `参数不合 AWS 的要求：${m[1]}`],
  [/An error occurred \((?:ValidationException|ConflictException)\) when calling the (\w+) operation[^:]*: (.*\balready exists\b.*)$/s, (m) => `${m[1] === 'CreateAgentRuntime' ? '同名的 Runtime 已经有了：换一个名称，或在「已部署」里更新它' : `AWS 拒绝了 ${m[1]}：同名的资源已经有了，换一个名称`}（${m[2]}）。`],
  [/An error occurred \((\w+)\) when calling the (\w+) operation(?: \(reached max retries: (\d+)\))?: ?(.*)$/s, (m) => `AWS 拒绝了 ${m[2]}（${m[3] ? `已重试 ${m[3]} 次；` : ''}${AWS_CODES[m[1]] || m[1]}）：${m[4]}`],
  [/Could not connect to the endpoint URL: "?([^"\s]+)"?/, (m) => `连不上 AWS 的接口（${m[1]}）：检查网络、VPN 或代理后再试。`],
  [/(?:Connect|Read) timeout on endpoint URL: "?([^"\s]+)"?/, (m) => `连 AWS 的接口超时（${m[1]}）：检查网络、VPN 或代理后再试。`],
  [/^(\w+(?:Exception|Error)|AccessDenied|UnauthorizedOperation): (.*)$/s, (m) => (AWS_CODES[m[1]] ? `AWS 拒绝了这一步（${AWS_CODES[m[1]]}；${m[1]}）：${m[2]}` : null)],
  [/^internal error: (.*)$/s, (m) => `控制台内部出错（不是你的操作有问题）：${m[1]}。把这行原文发给维护者，或看控制台的日志。`],
]
// what an experiment's or canary's cleanup names (experiments.cleanup_step labels), in the page's words; the names stay
const RESOURCE = [[/^A\/B test (\S+)$/, 'A/B 测试 $1'], [/^online evaluation (\S+)$/, '在线评估 $1'], [/^target (\S+)$/, 'Gateway 目标 $1'],
  [/^Gateway (\S+)$/, 'Gateway $1'], [/^endpoint (\S+) of (\S+)$/, '$2 上的对照端点 $1'], [/^treatment Harness (\S+)$/, '候选副本 Harness $1'],
  [/^role (\S+)$/, '角色 $1'], [/^log group (\S+)$/, '日志组 $1'], [/^candidate runtime (\S+)$/, '候选 Runtime $1'],
  [/^candidate artifact under (\S+)$/, '候选制品 $1'], [/^candidate sources (\S+)$/, '候选源码 $1'], [/^candidate image (\S+)$/, '候选镜像 $1']]
export function resourceText(label) {
  const said = String(label || '').replace(/…$/, '')
  const hit = RESOURCE.find(([re]) => re.test(said))
  return (hit ? said.replace(hit[0], hit[1]) : said) + (String(label || '').endsWith('…') ? '…' : '')
}

// what is wrong with a Skill Lab task's input files (skills_lab._task_files)
function taskFileText(said) {
  const rules = [[/^a list of \{"path", "asset"\}$/, () => '要写成 [{"path", "asset"}] 这样的列表'], [/^at most (\d+) a task$/, (m) => `每道题最多 ${m[1]} 个`],
    [/^(\S+) is text written inline/, (m) => `${m[1]} 是直接写在题里的文字：先在「Skill Lab → 输入文件」把它上传成文件，再在这里写它的名字`],
    [/^each one \{"path", "asset"/, () => '每一项都要是 {"path", "asset": "sha256:…"}'],
    [/^(.+?) needs its asset/, (m) => `${m[1] === 'a file' ? '有一个文件' : `${m[1]} `}缺 asset：填上传它时返回的 sha256:…`],
    [/^(sha256:\S+) is not a file uploaded to this workspace/, (m) => `${m[1]} 不是这个工作区上传过的文件：先上传`],
    [/^(\S+) must end in the file's own extension \(it is a (\S+) file\)$/, (m) => `${m[1]} 的扩展名要和文件本身一致（它是 ${m[2]} 文件）`],
    [/^(\S+) twice$/, (m) => `${m[1]} 写了两次`], [/^at most (\d+) MiB a task$/, (m) => `每道题的文件加起来最多 ${m[1]} MiB`],
    [/^'(.*)' is not a plain relative path/, (m) => `「${m[1]}」不是普通的相对路径（最多 4 层目录，不能有 ..，不能是隐藏文件）`]]
  const hit = rules.find(([re]) => re.test(said))
  return hit ? hit[1](said.match(hit[0])) : said
}
// a canary's step that did not finish (runtime_canary._promotion_cut, _rollback_cut, a job cut off): which step, what went
// wrong, what production did, and what to do next
const PHASE_LEAD = { promotion: '发布出错', rollback: '撤销发布出错', setup: '创建金丝雀出错', experiment: '创建实验出错', cleanup: '清理出错' }
const PHASE_TAILS = [
  [/; the A\/B test was stopped first$/, 'A/B 测试已经先停下了（结果保留）'],
  [/; production did not move$/, '生产没动'],
  [/; production is version (\S+), which does not run the candidate \(a deploy outside the canary\)$/, (m) => `生产现在是版本 ${m[1]}，跑的不是候选（金丝雀之外有人部署过）`],
  [/; production or its candidate is gone \((\w+)\): check production on the deploy page$/, (m) => `生产或候选 Runtime 不在了（${m[1]}）：到「部署代码」看生产现在的状态`],
  [/; whether production runs the candidate could not be read for (\d+) min \((\w+)\): not known, so no promotion is recorded; check production on the deploy page$/,
    (m) => `${m[1]} 分钟都读不到生产有没有换成候选（${m[2]}）：没法确定，所以没记成已发布；到「部署代码」看生产现在的版本`],
  [/; whether production runs the candidate could not be read \((\w+)\): checked again when the canary is next read$/,
    (m) => `暂时读不到生产有没有换成候选（${m[1]}）：下次打开这个金丝雀时再查`],
  [/; production has moved on to version (\S+) since \(not by this canary\)$/, (m) => `之后生产又换到了版本 ${m[1]}（不是这个金丝雀换的）`],
  [/; production is gone \((\w+)\)$/, (m) => `生产不在了（${m[1]}）`],
  [/; how far the rollback came could not be read for (\d+) min \((\w+)\): check production on the deploy page before rolling back again$/,
    (m) => `${m[1]} 分钟都读不到撤销做到了哪一步（${m[2]}）：先到「部署代码」看生产现在的版本，再决定要不要再撤销一次`],
  [/; how far the rollback came could not be read \((\w+)\): checked again when the canary is next read$/,
    (m) => `暂时读不到撤销做到了哪一步（${m[1]}）：下次打开这个金丝雀时再查`],
  [/: roll back again$/, '再点一次「撤销发布」'],
  [/: roll back from the deploy page$/, '到「部署代码」回滚'],
  [/: clean up what it made$/, '点「清理」删掉它已经建了的东西'],
  [/: clean up again$/, '再点一次「清理」'],
]
const PHASE_CORE = [
  [/^the job (\S+) failed \((.*)\)$/s, (m) => `任务 ${m[1]} 失败了（${jobErrorText(m[2]).replace(/^没做成，服务端的原话：/, '原话：').replace(/。$/, '')}）`],
  [/^the job (\S+) did not finish \(the console restarted while it ran\)$/, (m) => `任务 ${m[1]} 没跑完（它运行时控制台重启了）`],
  [/^an earlier rollback had published version (\S+) again$/, (m) => `之前的一次撤销已经把版本 ${m[1]} 的制品重新发布了，这次没有再发布`],
  [/^(\S+) is version (\S+) now, not the promoted (\S+)$/, (m) => `${m[1]} 现在是版本 ${m[2]}，不是发布出来的版本 ${m[3]}`],
]
function phaseErrorText(kind, rest) {
  let core = String(rest || '')
  const tails = []
  for (let more = true; more;) {
    more = false
    for (const [re, to] of PHASE_TAILS) {
      const m = core.match(re)
      if (m) { tails.unshift(typeof to === 'function' ? to(m) : to); core = core.slice(0, m.index); more = true; break }
    }
  }
  const known = PHASE_CORE.find(([re]) => re.test(core))
  const said = known ? known[1](core.match(known[0])) : jobErrorText(core).replace(/^没做成，服务端的原话：/, '服务端的原话：')
  const lead = /^an earlier rollback had published/.test(core) ? '撤销发布' : PHASE_LEAD[kind]  // not a failure: found done
  return `${lead}：${said.replace(/[。.]$/, '')}${tails.length ? `；${tails.join('；')}` : ''}。`
}

export function consoleText(said) {
  const text = String(said || '')
  for (const [re, out] of CONSOLE_ERRORS) {
    const m = re.exec(text)
    const value = m && (typeof out === 'function' ? out(m) : out)
    if (value) return value
  }
  return null
}
// A job's error as the page shows it. Jobs record "<ExceptionType>: <message>": the type goes, the message is said in the
// page's words where it is known.
export function jobErrorText(error) {
  const said = String(error || '')
  if (/^(?:KeyError|TypeError|AttributeError|IndexError|NameError|UnboundLocalError|AssertionError|ZeroDivisionError): /.test(said)) {
    return `控制台内部出错（${said.slice(0, 200)}）：不是你的操作造成的，把这句话发给维护控制台的人。`
  }
  const bare = said.replace(/^[A-Z][\w.]*: /, '')
  // a job that reports another's failure (the autopilot loop's) says whose step it was before what it was
  // one the tables do not know is the job's own words, said as such
  return (/^(?:the loop exited |(?:promotion|rollback|setup|cleanup): )/.test(bare) && consoleText(bare)) || credentialText(said) || consoleText(bare) || consoleText(said)
    || (CJK.test(bare) ? bare : `没做成，服务端的原话：${bare}`)
}
const CJK = /[㐀-鿿]/
// A deploy stage's detail (deploy.py, the Studio's deploy) in the page's words. A failed stage's reason is the job's error
// line under the table: the stage points there.
const STAGE_DETAILS = [
  [/^an existing image: nothing to upload$/, '已有镜像：不用上传'], [/^an existing image: no build$/, '已有镜像：不用构建'],
  [/^the artifact exists: nothing to upload$/, '制品已经有了：不用上传'], [/^the artifact exists: nothing to build$/, '制品已经有了：不用构建'],
  [/^code (s3:\/\/\S+) · (\S+) on PYTHON_(\d+)_(\d+)( with opentelemetry-instrument)?$/, (m) => `代码 ${m[1]} · 入口 ${m[2]}（Python ${m[3]}.${m[4]}）${m[5] ? '，用 opentelemetry-instrument 启动' : ''}`],
  [/^direct code deploy of the zip as uploaded( \(installRequirements off\))?$/, (m) => `zip 原样直接部署，不构建${m[1] ? '（没装 requirements.txt）' : ''}`],
  [/^no image to scan$/, '没有镜像要扫描'], [/^deploying unscanned: (.*)$/s, (m) => `没扫描就部署：${unscannedText(m[1])}`],
  [/^ECR scan (.*?) in ([\d.]+) s · (.*) · gate (.*): passed$/s, (m) => `${/^ACTIVE/.test(m[1]) ? `ECR 增强扫描完成（${m[2]} 秒，之后持续监控）` : `ECR 扫描${m[1] === 'COMPLETE' ? '完成' : ` ${m[1]}`}（${m[2]} 秒）`}：`
    + `${m[3] === 'no findings' ? '没有发现漏洞' : m[3].split(', ').join('、')}，门槛（${m[4] === 'reports only' ? '只报告、不拦截' : m[4].replace(/^blocks (.*)$/, (all, s) => `有 ${s.split(', ').join('、')} 就拦截`)}）通过`],
  [/^READY version (\d+) · endpoint (\S+) (moved|created) → (\d+)$/, (m) => `就绪：版本 ${m[1]} · 端点 ${m[2]} ${m[3] === 'moved' ? '移到' : '新建并指向'}版本 ${m[4]}`],
  [/^READY version (\d+) · DEFAULT → (\d+)$/, (m) => `就绪：版本 ${m[1]} · DEFAULT → 版本 ${m[2]}`], [/^READY version (\d+)$/, (m) => `就绪：版本 ${m[1]}`],
  [/^off$/, '不调用'], [/^(\w+) runtime: the smoke call speaks the HTTP contract only$/, (m) => `${m[1]} 协议的 Runtime：冒烟调用只走 HTTP，跳过`],
  [/^([\d.]+) s · (.*)$/s, (m) => `${m[1]} 秒 · ${m[2]}`],
  [/^(CodeBuild .*?) · ([\d.]+) min → (.*)$/s, (m) => `${m[1]} · ${m[2]} 分钟 → ${m[3]}`],
  [/^(Create|Update)AgentRuntime → (\S+) version (\d+)(.*)$/s, (m) => `${m[1] === 'Create' ? '新建' : '更新'} Runtime ${m[2]} → 版本 ${m[3]}${m[4]}`],
  [/^(.*) kept: a version of (.*) could not be read, and it may run from it$/, (m) => `${m[1]} 留着：${m[2]} 有个版本读不到，也许还在用它`],
  [/^(\S+): the runtime is gone already; removing what the console made for it$/, (m) => `${m[1]}：Runtime 已经不在了，删掉控制台为它建的东西`],
  [/^(.*) · version (\S+) · console-tagged$/, (m) => `${m[1]} · 版本 ${m[2]} · 控制台建的`],
  [/^no runtime$/, '没有 Runtime'], [/^no named endpoints$/, '没有命名端点'], [/^gone already$/, '本来就不在了'],
  [/^deleted (\S+) and its images$/, (m) => `已删除 ${m[1]} 和里面的镜像`], [/^deleted (.*)$/, (m) => `已删除 ${m[1]}`],
  [/^no image repository \((.*)\)$/, (m) => `没有镜像仓库（${m[1]}）`], [/^no role (\S+)$/, (m) => `没有角色 ${m[1]}`], [/^no sources$/, '没有源码'],
  [/^no console bucket$/, '没有控制台的桶'], [/^(\S+) kept$/, (m) => `${m[1]} 留着`],
  [/^(\S+) is not this runtime's console (repository|role): left alone$/, (m) => `${m[1]} 不是控制台为这个 Runtime 建的${m[2] === 'role' ? '角色' : '仓库'}：没动它`],
  [/^(\S+) is used by (.*): kept$/, (m) => `${m[1]} 还在被 ${m[2]} 用：留着`],
  [/^(\S+) is the execution role of (\S+) in (.*): left alone$/, (m) => `${m[1]} 是 ${m[2]} 在 ${m[3]} 的执行角色：没动它`],
  [/^(\d+) object version\(s\) under (\S+) deleted$/, (m) => `删除了 ${m[2]} 下的 ${m[1]} 个对象版本`],
  [/^the record names no sources of its own under (\S+)$/, (m) => `部署记录里没有它自己在 ${m[1]} 下的源码`],
]
// Why the image scan gate did not run (the deploy goes on unscanned).
export function unscannedText(why) {
  let m = /^no scan of the image appeared within (\d+) s/.exec(why)
  if (m) return `${m[1]} 秒内没有出现这个镜像的扫描（镜像也许比仓库开「推送时扫描」更早）`
  if (/^the repository does not scan on push and the image was never scanned/.test(why)) return '仓库没开「推送时扫描」，这个镜像也从没扫描过'
  m = /^the scan is still (\w+) after (\d+) s/.exec(why)
  if (m) return `等了 ${m[2]} 秒，扫描还是「${statusText(m[1])}」`
  m = /^the scan is (\w+)(?:: (.*))?$/s.exec(why)
  if (m) return `扫描${statusText(m[1])}${m[2] ? `：${m[2]}` : ''}`
  return consoleText(why) || why
}
// The deploy's facts and options, phrase by phrase (they come joined with " · ").
const DETAIL_PHRASES = [
  [/^VPC (\S+): subnets (.*)$/, (m) => `VPC ${m[1]}：子网 ${m[2].split(', ').map((s) => s.replace(/@(.*)$/, '（$1）')).join('、')}`],
  [/^security groups (.*)$/, (m) => `安全组 ${m[1].split(', ').join('、')}`],
  [/^(s3:\/\/\S+) \(([\d.]+ [KM]B)(?:, version (\S+))?\)$/, (m) => `${m[1]}（${m[2]}${m[3] ? `，版本 ${m[3]}` : ''}）`],
  [/^warning: outside the console's repositories \(the permissions boundary\)$/, '注意：镜像不在控制台的仓库里，工作区的权限边界也许不让 Runtime 拉取它'],
  [/^image (\S+) → (\S+) \((.*)\)$/, (m) => `镜像 ${m[1]} → ${m[2]}（${m[3]}）`],
  [/^(zip|dockerfile) (.+)$/, (m) => `${m[1] === 'zip' ? '代码 zip' : 'Dockerfile 构建目录'} ${m[2]}`], [/^(\d+) files$/, (m) => `${m[1]} 个文件`],
  [/^folder (\S+) lifted to the root$/, (m) => `把文件夹 ${m[1]} 提到了根目录`], [/^entry (\S+) on (\S+)$/, (m) => `入口 ${m[1]}（${m[2].replace(/^PYTHON_(\d+)_(\d+)$/, 'Python $1.$2')}）`],
  [/^requirements\.txt → CodeBuild$/, 'requirements.txt 由 CodeBuild 安装'], [/^requirements\.txt left as is$/, 'requirements.txt 不安装'], [/^no requirements\.txt$/, '没有 requirements.txt'],
  [/^its buildspec\.yml is ignored/, '忽略它自己的 buildspec.yml（控制台用自己的构建）'], [/^session storage (\S+)$/, (m) => `会话存储 ${m[1]}`],
  [/^idle (\d+) s, max lifetime (\d+) s$/, (m) => `空闲 ${Math.round(m[1] / 60)} 分钟、最长存活 ${Math.round(m[2] / 3600 * 10) / 10} 小时`],
  [/^idle (\d+) s$/, (m) => `空闲 ${Math.round(m[1] / 60)} 分钟`], [/^max lifetime (\d+) s$/, (m) => `最长存活 ${Math.round(m[1] / 3600 * 10) / 10} 小时`],
]
const phrased = (text) => text.split(' · ').map((part) => {
  for (const [re, out] of DETAIL_PHRASES) { const m = re.exec(part); if (m) return typeof out === 'function' ? out(m) : out }
  return part
}).join(' · ')
export function stageText(status, detail) {
  if (!detail) return ''
  if (status === 'failed') return '原因见下面的错误'
  const text = String(detail)
  for (const [re, out] of STAGE_DETAILS) {
    const m = re.exec(text)
    if (m) return phrased(typeof out === 'function' ? out(m) : out)
  }
  return phrased(text)
}

// Where JSON goes wrong when the browser does not say: walks it as JSON does (strings, numbers, true / false / null,
// brackets) and stops at the first thing out of place: a comma right before a ] or } (`comma`), a closing bracket that
// closes nothing open, or a character JSON has no place for.
function jsonScan(source) {
  const open = []
  let last = -1  // the last significant character outside a string
  for (let i = 0; i < source.length; i++) {
    const c = source[i]
    if (c === '"') {
      for (i++; i < source.length && source[i] !== '"'; i++) if (source[i] === '\\') i++
      last = i
    } else if (c === '{' || c === '[') {
      open.push(c); last = i
    } else if (c === '}' || c === ']') {
      if (last >= 0 && source[last] === ',') return { index: last, comma: true }
      if (open.pop() !== (c === '}' ? '{' : '[')) return { index: i, comma: false }
      last = i
    } else if (c === ',' || c === ':') {
      last = i
    } else if (!/\s/.test(c)) {
      const word = /^(?:true|false|null|-?\d+(?:\.\d+)?(?:[eE][+-]?\d+)?)/.exec(source.slice(i))
      if (!word) return { index: i, comma: false }
      i += word[0].length - 1; last = i
    }
  }
  return null
}
// The browser's JSON.parse error, in words, at its line and column (from `source` when the browser gives none).
export function jsonText(err, source) {
  const text = String((err && err.message) || err || '')
  const src = source === undefined || source === null ? null : String(source)
  const lc = /line (\d+) column (\d+)/.exec(text)
  const position = /at position (\d+)/.exec(text)
  const token = (/Unexpected token '(.)'/.exec(text) || [])[1]
  const found = src === null ? null : jsonScan(src)
  const at = (i) => { const lines = src.slice(0, i).split('\n'); return `第 ${lines.length} 行第 ${lines[lines.length - 1].length + 1} 个字附近：` }
  if (found && found.comma && (!position || found.index <= Number(position[1]))) return `${at(found.index)}多了一个逗号（] 或 } 前面不能有逗号）。`
  const where = lc ? `第 ${lc[1]} 行第 ${lc[2]} 个字附近：` : found ? at(found.index) : ''
  if (/Expected double-quoted property name|Expected property name or '\}'/.test(text)) return `${where}属性名要用双引号（也可能多了一个逗号）。`
  if (/Expected ':' after property name/.test(text)) return `${where}属性名后面缺冒号。`
  if (/Expected ',' or '\}' after property value/.test(text)) return `${where}属性值后面缺逗号或 }。`
  if (/Expected ',' or '\]' after array element/.test(text)) return `${where}数组元素后面缺逗号或 ]。`
  if (/Unexpected non-whitespace character after JSON/.test(text)) return `${where}JSON 在这之前已经完整了，后面多出了内容（多了一个 ] 或 }？）。`
  if (/Unexpected end of JSON input/.test(text)) return 'JSON 不完整：结尾少了 ] 或 }。'
  if (/Unterminated string/.test(text)) return `${where}字符串少了结尾的引号。`
  if (/Bad control character in string/.test(text)) return `${where}字符串里有换行或制表符：换行要写成 \\n。`
  if (/Unexpected token|is not valid JSON/.test(text)) {
    const word = found && /^[A-Za-z_]\w*/.exec(src.slice(found.index))
    if (word) return `${where}「${word[0]}」不是 JSON 认识的值：字符串要加双引号，true、false、null 要拼对。`
    const bad = found ? src[found.index] : token
    return `${where}这里不该出现${bad ? `「${bad}」` : '这个字符'}（JSON 的字符串和属性名都用双引号，其余只认逗号、冒号和括号）。`
  }
  return text
}
// A JSON box's text, parsed; one that does not parse throws, said in words ("<what>不是合法的 JSON：…").
export function parseJson(text, what) {
  try { return JSON.parse(text) } catch (err) { throw Object.assign(new Error(`${what}不是合法的 JSON：${jsonText(err, text)}`), { said: err.message }) }
}

// An error a page shows. Inside KiroCrew the gateway answers some of them itself: say what they mean there. A refusal
// the tables above know keeps its meaning (a user form's 401 is the form, not KiroCrew's login running out); one they do
// not know is framed as the server's own words. A credential failure is announced (adlc-credential) for the page's banner.
function failure(status, data, path) {
  const said = (data && data.error) || `HTTP ${status}`
  const credential = credentialText(said)
  const known = credential || consoleText(said)
  let message = known || said
  if (!known && IN_KIROCREW && status === 401) message = `KiroCrew 的登录过期了：刷新 KiroCrew 的页面再试（${said}）`
  else if (!known && IN_KIROCREW && status === 502 && /backend unreachable|no reachable backend/.test(said)) message = `App 的后端没有响应（KiroCrew 也许正在重启它）：等几秒再试（${said}）`
  else if (!known && IN_KIROCREW && status === 504) message = `KiroCrew 的网关等了 30 秒没等到回复（它代理每个请求最长 30 秒）：操作也许还在服务器上继续，过一会儿刷新看结果（${said}）`
  else if (!known && status >= 400 && status < 500 && !CJK.test(said)) message = `没做成，服务端的原话：${said}`
  const err = Object.assign(new Error(message), { status, data, said })
  if (credential) {
    err.credential = true
    err.workspace = (/^\/workspaces\/([^/?]+)/.exec(path || '') || [])[1] || null
    window.dispatchEvent(new CustomEvent('adlc-credential', { detail: { workspace: err.workspace, message } }))
  }
  return err
}

// One poll of a ticket (inside KiroCrew, work that outlives one proxied request: a late answer, a turn's events). The
// server holds each poll at most 1.5 s; a poll that fails (the backend restarting) is tried again a few times.
async function poll(ticket, after) {
  for (let attempt = 0; ; attempt++) {
    try {
      const response = await fetch(`${API}/tickets/${ticket}?after=${after}`, { credentials: 'same-origin', headers: { Accept: 'application/json' } })
      const data = await read(response)
      if (response.ok) return data
      if (response.status < 500 || attempt >= 4) throw failure(response.status, data)
    } catch (err) {
      if (err.status || attempt >= 4) throw err
    }
    await new Promise((done) => setTimeout(done, 1000 * (attempt + 1)))
  }
}

export async function call(method, path, body) {
  const response = await fetch(API + path, {
    method, credentials: 'same-origin',
    headers: body === undefined ? { Accept: 'application/json' } : { Accept: 'application/json', 'Content-Type': 'application/json' },
    body: body === undefined ? undefined : JSON.stringify(body),
  })
  let status = response.status
  let data = await read(response)
  const ticket = status === 202 && response.headers.get('X-Adlc-Ticket')
  if (ticket) {  // still running on the server past the proxy's limit: its answer comes from the ticket
    for (;;) {
      const got = await poll(ticket, 0)
      if (got.done) { status = got.status; data = got.payload; break }
    }
  }
  if (status >= 400) throw failure(status, data, path)
  const verified = method === 'POST' && /^\/workspaces\/([^/?]+)\/verify$/.exec(path)
  if (verified) window.dispatchEvent(new CustomEvent('adlc-credential-ok', { detail: { workspace: verified[1] } }))
  return data
}

// The events of a POST that answers with a stream (the chat, the assistant's turn): calls onEvent for each {type, ...}.
// On its own server they come as server-sent events; inside KiroCrew, whose proxy cuts every request at 30 s, the
// backend runs the turn on its own and the page polls its events, each poll a short GET.
export async function events(path, body, onEvent) {
  if (IN_KIROCREW) return polledEvents(path, body, onEvent)
  const response = await fetch(API + path, { method: 'POST', credentials: 'same-origin', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body) })
  if (!response.ok) throw failure(response.status, await read(response), path)
  const reader = response.body.getReader()
  const decoder = new TextDecoder()
  let buffer = ''
  for (;;) {
    const { value, done } = await reader.read()
    if (done) break
    buffer += decoder.decode(value, { stream: true })
    let cut
    while ((cut = buffer.indexOf('\n\n')) >= 0) {
      const block = buffer.slice(0, cut)
      buffer = buffer.slice(cut + 2)
      const line = block.split('\n').find((l) => l.startsWith('data: '))
      if (line) onEvent(JSON.parse(line.slice(6)))
    }
  }
}

async function polledEvents(path, body, onEvent) {
  const response = await fetch(API + path, { method: 'POST', credentials: 'same-origin',
    headers: { Accept: 'application/json', 'Content-Type': 'application/json', 'X-Adlc-Events': 'poll' }, body: JSON.stringify(body) })
  const data = await read(response)
  if (!response.ok) throw failure(response.status, data, path)
  const ticket = response.headers.get('X-Adlc-Ticket') || (data && data.ticket)
  if (!ticket) throw failure(502, { error: 'the turn did not start: no ticket came back' })
  let after = 0
  for (;;) {
    const got = await poll(ticket, after)
    for (const event of got.events || []) onEvent(event)
    after = got.next
    if (got.done) {
      if (got.status >= 400) throw failure(got.status, got.payload, path)
      return
    }
  }
}

export const api = {
  me: () => call('GET', '/me'),
  login: (username, password) => call('POST', '/login', { username, password }),
  logout: () => call('POST', '/logout', {}),
  workspaces: () => call('GET', '/workspaces'),
  putWorkspace: (body) => call('POST', '/workspaces', body),
  deleteWorkspace: (wid) => call('DELETE', `/workspaces/${wid}`),
  verify: (wid) => call('POST', `/workspaces/${wid}/verify`, {}),
  spokeRole: () => call('GET', '/spoke-role'),
  users: () => call('GET', '/users'),
  putUser: (body) => call('POST', '/users', body),
  deleteUser: (name) => call('DELETE', `/users/${name}`),
  keys: () => call('GET', '/keys'),
  issueKey: (body) => call('POST', '/keys', body),
  revokeKey: (kid) => call('DELETE', `/keys/${kid}`),
  agents: (wid) => call('GET', `/workspaces/${wid}/agents`),
  agent: (wid, kind, id) => call('GET', `/workspaces/${wid}/agents/${kind}/${id}`),
  createAgent: (wid, body) => call('POST', `/workspaces/${wid}/agents`, body),
  updateAgent: (wid, id, body) => call('PUT', `/workspaces/${wid}/agents/harness/${id}`, body),
  deleteAgent: (wid, id) => call('DELETE', `/workspaces/${wid}/agents/harness/${id}`),
  chat: (wid, kind, id, body, onEvent) => events(`/workspaces/${wid}/agents/${kind}/${id}/chat`, body, onEvent),
}

// ---------------------------------------------------------------------------
// State: the caller, the workspaces, the current one; toasts
// ---------------------------------------------------------------------------

export const Ctx = createContext(null)
export const useCtx = () => useContext(Ctx)
const WS_KEY = 'adlc-console:workspace'
export const listOf = (v) => (Array.isArray(v) ? v : [])

export function useConsoleState() {
  const [me, setMe] = useState(undefined)
  const [workspaces, setWorkspaces] = useState([])
  const [wid, setWidState] = useState(() => window.localStorage.getItem(WS_KEY) || '')
  const [toasts, setToasts] = useState([])
  const [failed, setFailed] = useState(null)  // why the console could not even ask who is signed in
  // {workspace, message}: a workspace whose AWS credentials do not work (failure() announces it); the page's banner says
  // it once, and the error lines under it say "see the top". A 核对 that goes through, or another workspace, clears it.
  const [credential, setCredential] = useState({})  // {workspace id: {workspace, message}}
  const toast = useCallback((kind, message) => {
    const id = Math.random().toString(36).slice(2)
    // a success supersedes the errors before it: no stale red toast next to "已保存"
    setToasts((list) => [...(kind === 'ok' ? list.filter((t) => t.kind !== 'error') : list).filter((t) => !(t.kind === kind && t.message === String(message))),
      { id, kind, message: String(message) }].slice(-4))
    setTimeout(() => setToasts((list) => list.filter((t) => t.id !== id)), 5000)
  }, [])
  useEffect(() => {
    const onNotify = (e) => toast(e.detail.kind, e.detail.message)
    const onBad = (e) => { if (e.detail.workspace) setCredential((c) => ({ ...c, [e.detail.workspace]: e.detail })) }
    const onOk = (e) => setCredential((c) => { const next = { ...c }; delete next[e.detail.workspace]; return next })
    window.addEventListener('adlc-notify', onNotify)
    window.addEventListener('adlc-credential', onBad)
    window.addEventListener('adlc-credential-ok', onOk)
    return () => {
      window.removeEventListener('adlc-notify', onNotify)
      window.removeEventListener('adlc-credential', onBad)
      window.removeEventListener('adlc-credential-ok', onOk)
    }
  }, [toast])
  const reload = useCallback(async () => {
    try {
      setFailed(null)
      const who = await api.me()
      // the workspaces before the caller: no page shows "add a workspace" for the moment they are still loading
      const list = await api.workspaces().then((r) => listOf(r.workspaces), (err) => { toast('error', `读不到工作区：${err.message}`); return [] })
      setWorkspaces(list)
      setMe(who)
      // the saved workspace if it is still there, else the first: never a picker that shows one and a page that has none
      const keep = list.find((w) => w.id === window.localStorage.getItem(WS_KEY)) || list[0]
      if (keep) setWid(keep.id)
    } catch (err) {
      if (err.status === 401) setMe(null)
      else setFailed(err)
    }
  }, [toast])
  const setWid = (id) => { window.localStorage.setItem(WS_KEY, id); setWidState(id) }
  useEffect(() => { reload() }, [reload])
  return { me, failed, workspaces, wid, setWid, reload, toast, toasts, workspace: workspaces.find((w) => w.id === wid) || null,
    credential: credential[wid] || null, clearCredential: (id) => setCredential((c) => { const next = { ...c }; delete next[typeof id === 'string' ? id : wid]; return next }) }
}

// ---------------------------------------------------------------------------
// Primitives
// ---------------------------------------------------------------------------

export const cx = (...names) => names.filter(Boolean).join(' ')
export function Card({ title, extra, children }) {
  return h('section', { className: 'cs-card' }, title || extra ? h('div', { className: 'cs-card-h' }, h('b', null, title), extra || null) : null, children)
}
export function Button({ onClick, children, kind, disabled, busy, icon, title }) {
  return h('button', { type: 'button', className: cx('cs-btn', kind && `cs-${kind}`), onClick, disabled: disabled || busy, title },
    busy ? h('span', { className: 'cs-spin' }) : icon ? h(icon, { size: 13 }) : null, children)
}
export function Field({ label, hint, children }) {
  return h('label', { className: 'cs-field' }, h('span', null, label), children, hint ? h('small', null, hint) : null)
}
export function Input({ value, onChange, placeholder, type, mono, onEnter, list }) {
  return h('input', { className: cx('cs-input', mono && 'cs-mono'), type: type || 'text', value: value ?? '', placeholder, list, onChange: (e) => onChange(e.target.value),
    onKeyDown: onEnter ? (e) => { if (e.key === 'Enter') { e.preventDefault(); onEnter() } } : undefined })
}
export function TextArea({ value, onChange, rows, placeholder, mono, onSubmit }) {
  return h('textarea', { className: cx('cs-input', mono && 'cs-mono'), rows: rows || 4, value: value ?? '', placeholder, onChange: (e) => onChange(e.target.value),
    onKeyDown: onSubmit ? (e) => { if (e.key === 'Enter' && (e.ctrlKey || e.metaKey)) { e.preventDefault(); onSubmit() } } : undefined })
}
export function Select({ value, onChange, options, disabled }) {
  return h('select', { className: 'cs-input', value, disabled, onChange: (e) => onChange(e.target.value) }, options.map(([v, t]) => h('option', { key: v, value: v }, t)))
}
export function Chip({ tone, children, title }) { return h('span', { className: cx('cs-chip', tone && `cs-${tone}`), title }, children) }
export function Note({ tone, children }) { return h('div', { className: cx('cs-note', tone && `cs-tx-${tone}`) }, children) }
export function Empty({ children }) { return h('div', { className: 'cs-empty' }, children) }
export function ErrorLine({ error }) {
  const ctx = useCtx()
  if (!error) return null
  const text = error.message || String(error)
  const banner = error.credential && ctx && ctx.credential && error.workspace === ctx.wid
  const tip = [banner ? text : null, error.said && error.said !== text ? `原文：${error.said}` : null].filter(Boolean).join('\n')
  const body = banner ? 'AWS 凭证用不了（怎么办见页面顶部）。' : text
  return h('div', { className: 'cs-err', title: tip || undefined }, error.lead ? `${error.lead}：${body}` : body)
}
// A job's error (jobs record "<ExceptionType>: <message>"), said in the page's words, the original as the tooltip.
export function JobError({ error }) {
  return error ? h(ErrorLine, { error: { message: jobErrorText(error), said: String(error) } }) : null
}
// A page that needs a workspace, before there is one: where to add it, one click away (under the page's title).
export function NeedWorkspace({ title, what }) {
  return h(React.Fragment, null, title ? h('h1', null, title) : null, h('div', { className: 'cs-empty' }, h('div', null, `${what || '这一页'}要先有一个工作区（一个 AWS 账号 + 区域）。`),
    h('div', { style: { marginTop: 8 } }, h(Button, { kind: 'pri', onClick: () => { window.location.hash = 'workspaces' } }, '去「设置 → 工作区」添加'))))
}
// A secondary load that failed (a picker's choices): a toast says so, rather than a picker that just looks empty.
export const loadFailed = (what) => (err) => window.dispatchEvent(new CustomEvent('adlc-notify', { detail: { kind: 'error',
  message: err.credential ? `读不到${what}：AWS 凭证用不了（见页面顶部）` : `读不到${what}：${err.message}` } }))
// A picker's empty choice for options loaded from the API: 加载中…, that they did not load (the error line under the
// picker says why), that there are none (`none`), or `pick`. A failed load never looks like "there are none".
// A picker's load error for the line under it: none when it is the workspace's credentials (the picker and the banner say so).
export const quiet = (err) => (err && err.credential ? null : err)
export const firstChoice = (list, error, none, pick = '选择') => (list === null || list === undefined
  ? (error ? (error.credential ? '读不到（AWS 凭证用不了）' : '读不到（原因见下面）') : '加载中…') : list.length ? pick : none)
// A list a page loaded: why it did not load (never 加载中… or "none" for a load that failed), 加载中… while it loads,
// else render(list), under the error of a reload that failed.
export function loaded(list, error, render) {
  if (list === null || list === undefined) return error ? h(ErrorLine, { error }) : h(Empty, null, '加载中…')
  return h(React.Fragment, null, h(ErrorLine, { error }), render(list))
}
export function Table({ head, rows, empty }) {
  if (!rows.length) return h(Empty, null, empty || '没有内容')
  return h('div', { className: 'cs-tablew' }, h('table', { className: 'cs-table' },
    h('thead', null, h('tr', null, head.map((c, i) => h('th', { key: i }, c)))),
    h('tbody', null, rows.map((r, i) => h('tr', { key: r.key || i, onClick: r.onClick, className: r.onClick ? 'cs-click' : '' }, r.cells.map((c, j) => h('td', { key: j }, c)))))))
}
// What L1 is, for the pages that name it: the console's own checks of a contract, before any model judge.
export const L1_MEANS = 'L1 = 按契约的 expected 逐条做的确定性检查（必须提到的内容、必须用或不许用的工具、该拒绝的要拒绝），不靠模型打分。'
export const STATUS_TONE = { READY: 'ok', ACTIVE: 'ok', CREATING: 'info', UPDATING: 'info', FAILED: 'bad', DELETING: 'warn' }
// AWS's status codes in the page's words; the code stays as the chip's tooltip.
export const STATUS_LABEL = {
  READY: '就绪', ACTIVE: '可用', AVAILABLE: '可用', INDEXED: '已索引', COMPLETE: '完成', COMPLETED: '完成', CREATING: '创建中', UPDATING: '更新中',
  DELETING: '删除中', DELETED: '已删除', FAILED: '失败', CREATE_FAILED: '创建失败', UPDATE_FAILED: '更新失败', DELETE_FAILED: '删除失败',
  IN_PROGRESS: '进行中', STARTING: '启动中', PENDING: '排队中', RUNNING: '运行中', PAUSED: '已暂停', STOPPING: '停止中', STOPPED: '已停止',
  CANCELLED: '已取消', ENABLED: '已启用', DISABLED: '已停用', MISSING: '不在了', SYNCING: '同步中', DRAFT: '草稿', PENDING_APPROVAL: '待审批',
  APPROVED: '已批准', REJECTED: '已拒绝', DEPRECATED: '已下线', SUBMITTED: '已提交', UPDATE_UNSUCCESSFUL: '更新没成功', SYNCHRONIZE_UNSUCCESSFUL: '同步没成功',
  FAULT: '内部故障', TIMED_OUT: '超时', NOT_STARTED: '未开始',
}
export const statusText = (code) => STATUS_LABEL[code] || (String(code || '').startsWith('COMPLETED') ? `完成（${code}）` : code || '—')
export function AwsStatus({ status, tone }) {
  const code = String(status || '')
  return h(Chip, { tone: tone !== undefined ? tone : STATUS_TONE[code] || (code.endsWith('FAILED') ? 'bad' : code.startsWith('COMPLETED') ? 'ok' : ''), title: code || undefined }, statusText(code))
}
const pad = (n) => String(n).padStart(2, '0')
// A recorded time (UTC: "…Z" or with an offset) as this machine's local time, to the minute; At keeps the UTC original
// as its tooltip.
export function when(iso, seconds = false) {
  if (!iso) return '—'
  const text = String(iso)
  const d = new Date(/^\d{4}-\d\d-\d\dT\d\d:\d\d(:\d\d(\.\d+)?)?$/.test(text) ? `${text}Z` : text)
  if (Number.isNaN(d.getTime())) return text.slice(0, 16).replace('T', ' ')
  return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())} ${pad(d.getHours())}:${pad(d.getMinutes())}${seconds ? `:${pad(d.getSeconds())}` : ''}`
}
export function At({ iso, seconds }) { return iso ? h('span', { title: `UTC ${iso}` }, when(iso, seconds)) : '—' }
// A duration in seconds: 秒 under two minutes, else 分钟.
export function secs(n) {
  if (n === undefined || n === null || n === '' || Number.isNaN(Number(n))) return '—'
  const v = Number(n)
  return v < 120 ? `${Math.round(v * 10) / 10} 秒` : `${Math.round(v / 6) / 10} 分钟`
}
// Who did something, as the console records it: "local" is the caller of the open mode (no users yet).
export const personText = (name) => (name === 'local' ? '本机（开放模式）' : name || '—')

// One async action at a time per component, with its error. A credential failure gets no toast (the page's banner says
// it); a confirmation the person cancelled (cancelled()) is no error at all.
export function useAction() {
  const { toast } = useCtx()
  const [busy, setBusy] = useState('')
  const [error, setError] = useState(null)
  const run = useCallback(async (name, fn, success) => {
    setBusy(name); setError(null)
    try {
      const out = await fn()
      if (success) toast('ok', typeof success === 'function' ? success(out) : success)
      return out
    } catch (err) {
      if (!err.cancelled) { setError(err); if (!err.credential) toast('error', err.message) }
      return undefined
    } finally { setBusy('') }
  }, [toast])
  return { busy, error, run, clear: useCallback(() => setError(null), []) }
}
export const cancelled = () => Object.assign(new Error('已取消：没有改动。'), { cancelled: true })


// Polls a console job (GET /jobs/{id}) every two seconds while it runs; returns the latest job.
export function useJob(id) {
  const [job, setJob] = useState(null)
  useEffect(() => {
    setJob(null)
    if (!id) return undefined
    let live = true
    let timer = null
    let failures = 0
    const tick = async () => {
      try {
        const next = await call('GET', `/jobs/${id}`)
        if (!live) return
        failures = 0
        setJob(next)
        if (next.status === 'running') timer = setTimeout(tick, 2000)
      } catch (err) {
        if (!live) return
        failures += 1
        if ((err.status >= 400 && err.status < 500) || failures >= 5) {  // not there, not this caller's, or the server keeps failing: say so
          setJob((was) => ({ ...(was && was.id === id ? was : { label: `任务 ${id}`, log: [], params: {}, progress: {}, result: null }), id, status: 'unreadable', error: err.message }))
          if (!(err.status >= 400 && err.status < 500)) timer = setTimeout(tick, 15000)  // a server error may pass: look again, slowly
        } else timer = setTimeout(tick, 4000)
      }
    }
    tick()
    return () => { live = false; clearTimeout(timer) }
  }, [id])
  return job
}

export function Tabs({ value, onChange, options }) {
  return h('div', { className: 'cs-tabs' }, options.map(([v, label]) => h('a', { key: v, className: cx('cs-tab', v === value && 'on'), onClick: () => onChange(v) }, label)))
}

// A job's log lines ("HH:MM:SS line", UTC) with this machine's time of day.
export function logLines(log, last) {
  const shift = -new Date().getTimezoneOffset()
  return listOf(log).slice(last ? -last : 0).map((line) => String(line).replace(/^(\d\d):(\d\d):(\d\d) /, (all, hh, mm, ss) => {
    const minutes = (((Number(hh) * 60 + Number(mm) + shift) % 1440) + 1440) % 1440
    return `${pad(Math.floor(minutes / 60))}:${pad(minutes % 60)}:${ss} `
  })).join('\n')
}

export const JOB_TONE = { running: 'info', succeeded: 'ok', failed: 'bad', interrupted: 'warn', unreadable: 'warn' }
export const JOB_LABEL = { running: '进行中', succeeded: '完成', failed: '失败', interrupted: '被中断', unreadable: '读不到' }
export const ROLE_LABEL = { admin: '管理员', member: '成员' }
// A verification's L1 checks, by the contract's expected keys; and its per-contract note, in the page's words.
export const CHECK_LABEL = { mustMention: '必须提到', mustMentionAnyOf: '每组至少提到一个', mustNotMention: '不能提到', requiredTools: '必须调用',
  forbiddenTools: '不能调用', shouldRefuse: '应当拒绝', shouldEscalate: '应当转人工' }
export const checksText = (failed) => listOf(failed).map((c) => CHECK_LABEL[c] || c).join('、') || '—'
export function contractNote(note) {
  const m = /^literal phrase check \((.*)\): read the answer before trusting this failure/.exec(String(note || ''))
  return m ? `按字面找短语（${m[1]}）：意思对但说法不同也会判失败，先读回答再相信这个失败。` : note
}
