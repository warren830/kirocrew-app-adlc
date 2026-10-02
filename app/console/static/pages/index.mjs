// The console's capability pages, one module each ({id, label, group, Page}); the shell adds them to its navigation.
import knowledge from './knowledge.mjs'
import evaluation from './evaluation.mjs'
import experiments from './experiments.mjs'
import registry from './registry.mjs'
import governance from './governance.mjs'
import observability from './observability.mjs'
import deploy from './deploy.mjs'
import skills from './skills.mjs'
import studio from './studio.mjs'
import assistant from './assistant.mjs'
import autopilot from './autopilot.mjs'

export const MODULES = [assistant, deploy, studio, knowledge, skills, autopilot, evaluation, experiments, registry, governance, observability].filter(Boolean)
