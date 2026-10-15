// Cloudflare Worker: trigger the Blackwell monitor on a reliable schedule.
// GitHub's own `schedule` trigger is best effort and often skips runs; a
// `workflow_dispatch` starts immediately. There is deliberately no `fetch`
// handler, so the Worker URL cannot be used to trigger runs.
const REPO = "JigaoLuo/brev-blackwell-monitor";
const WORKFLOW = "monitor.yml";

export default {
  async scheduled(event, env, ctx) {
    const response = await fetch(
      `https://api.github.com/repos/${REPO}/actions/workflows/${WORKFLOW}/dispatches`,
      {
        method: "POST",
        headers: {
          Accept: "application/vnd.github+json",
          Authorization: `Bearer ${env.GITHUB_TOKEN}`,
          "User-Agent": "brev-blackwell-monitor-dispatch",
          "X-GitHub-Api-Version": "2022-11-28",
        },
        body: JSON.stringify({ ref: "main" }),
        redirect: "manual",
      },
    );
    if (!response.ok) {
      // Throwing marks the cron invocation as failed in the Cloudflare dashboard.
      throw new Error(`GitHub workflow dispatch failed: HTTP ${response.status}`);
    }
  },
};
