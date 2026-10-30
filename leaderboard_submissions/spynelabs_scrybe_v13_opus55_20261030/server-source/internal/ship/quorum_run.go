package ship

import (
	"context"
	"crypto/rand"
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"fmt"
	"slices"
	"strconv"
	"strings"
	"sync"
	"sync/atomic"
	"time"

	"github.com/multica-ai/multica/server/internal/quorum"
	db "github.com/multica-ai/multica/server/pkg/db/generated"
)

// Quorum engine (docs/superpowers/specs/2026-10-19-quorum-harness-design.md):
// the deterministic kernel in internal/quorum decides; this runner carries
// out its actions in the run's IDE sandbox (sessions, gates, merges, pushes)
// and reports results back. It never decides.

// EngineQuorum marks runs driven by the Quorum kernel.
const EngineQuorum = "quorum"

const qIntegrationDir = gsWorkersDir + "/q-integration"

type qSess struct {
	asg       string
	sessionID string
	dir       string
	token     string
	started   time.Time
	nudged    bool
}

type qRun struct {
	s     *Service
	host  GitSwarmHost
	wsID  string
	runID string
	obj   Objective
	run   Run
	sink  EventSink
	ideID string
	start time.Time
	k     *quorum.Kernel
	g     *gsRun // push and merge machinery shared with GitSwarm

	mu      sync.Mutex
	sess    map[string]*qSess   // assignment -> session
	tokens  map[string]string   // sha256(token) -> assignment
	notices map[string][]string // assignment -> kernel notices not yet shown
	busy    map[string]bool     // claim cids with a gate in flight

	mergeMu  sync.Mutex
	cancel   context.CancelFunc
	abort    atomic.Bool
	stopMsg  string
	wake     chan struct{}
	finished atomic.Bool
}

func (s *Service) qLiveRun(runID string) *qRun {
	s.mu.Lock()
	defer s.mu.Unlock()
	return s.qRuns[runID]
}

func (q *qRun) runShort() string { return q.runID[:min(8, len(q.runID))] }

func (q *qRun) integrationBranch() string { return "quorum/" + q.runShort() + "/integration" }

func (q *qRun) exec(ctx context.Context, script string, args ...string) (string, error) {
	return q.host.SandboxExec(ctx, q.ideID, append([]string{"sh", "-c", script, "sh"}, args...))
}

func (q *qRun) emit(ctx context.Context, e Event) { q.sink.Emit(ctx, q.wsID, q.runID, e) }

func (q *qRun) poke() {
	select {
	case q.wake <- struct{}{}:
	default:
	}
}

// --- setup ---

// qSetupScript records the base branch, creates the integration branch and
// worktree, and installs the scope hook. $1 integration branch, $2 hooks dir.
const qSetupScript = `set -e
cd /home/ide/workspace
base=$(git symbolic-ref --short -q HEAD || true)
[ -n "$base" ] && git config quorum.base "$base"
git rev-parse -q --verify HEAD >/dev/null
git checkout -q --detach
if ! git rev-parse -q --verify "refs/heads/$1" >/dev/null; then git branch -q "$1" HEAD; fi
git worktree prune
if [ ! -d /home/ide/swarm/q-integration ]; then
  mkdir -p /home/ide/swarm
  git worktree add -q /home/ide/swarm/q-integration "$1" >/dev/null 2>&1 || true
fi
prev=$(git config --get core.hooksPath || true)
mkdir -p "$2"
if [ "$prev" != "$2" ]; then git config quorum.prevhooks "${prev:-.git/hooks}"; fi
cat > "$2/pre-commit" <<'HOOK'
#!/bin/sh
# Quorum scope enforcement: commits from an assignment must stay inside its
# admitted intent. Commits without a capability token (the harness) pass.
if [ -n "$Q_TOKEN" ] && command -v q_precommit >/dev/null 2>&1; then
  q_precommit || exit 1
fi
prev=$(git config --get quorum.prevhooks || true)
case "$prev" in /*) ;; *) prev="$(git rev-parse --show-toplevel)/$prev" ;; esac
if [ -x "$prev/pre-commit" ]; then exec "$prev/pre-commit" "$@"; fi
exit 0
HOOK
chmod +x "$2/pre-commit"
git config core.hooksPath "$2"
base=$(git config --get quorum.base || true)
printf 'BASE %s\n' "$base"
printf 'HEAD %s\n' "$(git rev-parse "$1")"`

// runQuorum drives one run. A run with a ledger resumes from it.
func (s *Service) runQuorum(parent context.Context, wsID, runID string) {
	sink := NewEventSink(s.store, s.cast)
	run, err := s.store.GetRunByID(parent, runID)
	if err != nil {
		return
	}
	fail := func(reason string) {
		run.Status, run.Error, run.EndedAt, run.VerdictSentence = RunStuck, reason, time.Now(), reason
		run.Result = Assessment{Achieved: false, Narrative: reason}
		_ = s.store.UpdateRunSwarm(parent, run)
		sink.Emit(parent, wsID, runID, Event{Type: EvRunFailed, Level: "error", Title: reason})
	}
	obj, err := s.store.GetObjective(parent, wsID, run.ObjectiveID)
	if err != nil {
		fail("could not load objective: " + err.Error())
		return
	}
	if s.gs == nil {
		fail("Quorum needs the Alpha Sygnal IDE (set MULTICA_IDE_ENABLED=true)")
		return
	}
	ctx, cancel := context.WithCancel(parent)
	defer cancel()
	q := &qRun{
		s: s, host: s.gs, wsID: wsID, runID: runID, obj: obj, run: run, sink: sink, start: time.Now(),
		sess: map[string]*qSess{}, tokens: map[string]string{}, notices: map[string][]string{}, busy: map[string]bool{},
		cancel: cancel, wake: make(chan struct{}, 1),
	}
	q.g = &gsRun{s: s, host: s.gs, wsID: wsID, runID: runID, obj: obj, run: run, sink: sink, start: q.start,
		live: map[string]*gsLive{}, published: map[string]string{}, unmerged: map[string]string{}, capSB: map[string]Sandbox{}}
	s.mu.Lock()
	s.qRuns[runID] = q
	s.mu.Unlock()
	defer func() {
		s.mu.Lock()
		delete(s.qRuns, runID)
		s.mu.Unlock()
		s.gs.ReleasePushMirror(runID)
	}()

	q.emit(ctx, Event{Type: EvRunStarted, Title: "Quorum: " + obj.Title, Body: map[string]any{"engine": EngineQuorum}})
	q.g.credEnv = credentialEnv(ctx, s, wsID, obj.Config, q.emit)
	defer q.g.teardownCaps()
	q.g.owner, _ = obj.Config["created_by"].(string)
	q.g.repoURL = objectiveRepoURL(obj.Config)
	squadID, _ := obj.Config["squad_id"].(string)
	ideID, err := q.host.EnsureGitSwarmSandbox(ctx, wsID, runID, q.g.repoURL, squadID, q.g.owner)
	if err != nil {
		fail("the swarm sandbox did not start: " + err.Error())
		return
	}
	q.ideID, q.g.ideID = ideID, ideID
	q.host.HoldSandbox(ideID, true)
	defer q.host.HoldSandbox(ideID, false)
	out, err := q.exec(ctx, qSetupScript, q.integrationBranch(), "/home/ide/.quorum/hooks")
	if err != nil {
		fail("could not prepare the repository: " + truncate(lastLineStr(out)+" "+err.Error(), 300))
		return
	}
	base, head := "", ""
	for _, line := range strings.Split(out, "\n") {
		if v, ok := strings.CutPrefix(line, "BASE "); ok {
			base = strings.TrimSpace(v)
		}
		if v, ok := strings.CutPrefix(line, "HEAD "); ok {
			head = strings.TrimSpace(v)
		}
	}
	q.g.baseBranch = base
	if err := q.loadKernel(ctx, head, base, squadID); err != nil {
		fail("could not start the Quorum kernel: " + err.Error())
		return
	}
	go q.g.pushRefs(context.WithoutCancel(ctx), []GsPushRef{{Branch: q.integrationBranch(), SHA: head, Force: false}}, nil)
	q.loop(ctx)
	q.finalize(parent)
}

// loadKernel replays the ledger or creates a new run.
func (q *qRun) loadKernel(ctx context.Context, head, base, squadID string) error {
	opts := quorum.Options{Sink: q.persist}
	rows, err := q.s.store.q.ListQuorumEvents(ctx, pgUUID(q.runID))
	if err != nil {
		return err
	}
	if len(rows) > 0 {
		events := make([]quorum.Event, 0, len(rows))
		for _, r := range rows {
			var e quorum.Event
			if err := json.Unmarshal([]byte(r.Body), &e); err != nil {
				return fmt.Errorf("ledger event %d: %w", r.Seq, err)
			}
			events = append(events, e)
		}
		k, err := quorum.Replay(events, opts)
		if err != nil {
			return err
		}
		q.k = k
		for _, a := range k.ActiveAssignments() {
			k.SessionEnded(a, false, "the server restarted during this assignment")
		}
		q.emit(ctx, Event{Type: EvRunStarted, Title: fmt.Sprintf("resumed from %d ledger events", len(events))})
		return nil
	}
	in, err := q.newRunInput(ctx, head, base, squadID)
	if err != nil {
		return err
	}
	k, err := quorum.NewRun(in, opts)
	if err != nil {
		return err
	}
	q.k = k
	for _, l := range in.Lineages {
		_ = q.s.store.q.UpsertQuorumLineage(ctx, db.UpsertQuorumLineageParams{WorkspaceID: pgUUID(q.wsID), LineageID: l.ID, Family: l.Family, Model: l.Model, Harness: l.Harness, PersonaHash: l.PersonaHash, Label: l.Label})
	}
	return nil
}

// newRunInput turns the objective into a goal, charter and lineages.
func (q *qRun) newRunInput(ctx context.Context, head, base, squadID string) (quorum.NewRunInput, error) {
	cfg, _ := q.obj.Config["quorum"].(map[string]any)
	doc, _ := cfg["charter_yaml"].(string)
	ch, err := quorum.ParseCharter(doc)
	if err != nil {
		return quorum.NewRunInput{}, err
	}
	if m, _ := cfg["mode"].(string); m == "lite" || m == "market" {
		ch.Mode = m
	}
	b := gsBudgetFrom(q.run.Budget, q.obj.Config)
	if strings.TrimSpace(doc) == "" || !strings.Contains(doc, "budget") {
		ch.Run.Budget.USD, ch.Run.Budget.WallSec = b.SpendCapUSD, b.WallClockSec
	}
	if strings.TrimSpace(doc) == "" || !strings.Contains(doc, "max_agents") {
		ch.Run.MaxAgents = b.Concurrency
	}
	ch.Run.AssignmentSec = b.EpisodeSec
	// An attached squad supplies the lineages: one per agent persona.
	if squadID != "" {
		var personas []quorum.CharterLineage
		qq := q.s.store.q
		if sq, err := qq.GetSquadInWorkspace(ctx, db.GetSquadInWorkspaceParams{ID: pgUUID(squadID), WorkspaceID: pgUUID(q.wsID)}); err == nil {
			members, _ := qq.ListSquadMembers(ctx, sq.ID)
			for _, m := range members {
				if m.MemberType != "agent" {
					continue
				}
				a, err := qq.GetAgent(ctx, m.MemberID)
				if err != nil || a.WorkspaceID != sq.WorkspaceID || a.ArchivedAt.Valid {
					continue
				}
				model := firstNonEmpty(a.Model.String, "claude-sonnet-5-5")
				personas = append(personas, quorum.CharterLineage{Family: qFamily(model), Model: model, Harness: "opencode", Slots: 1, Persona: truncate(a.Instructions, 4000), Label: a.Name, AgentID: uuidStr(a.ID)})
			}
		}
		if len(personas) > 0 {
			if len(personas) == 1 {
				personas[0].Slots = 2
			}
			ch.Lineages = personas
		}
	}
	if err := ch.Validate(); err != nil {
		return quorum.NewRunInput{}, err
	}
	var lineages []quorum.Lineage
	var ids []string
	for _, l := range ch.Lineages {
		ln := quorum.LineageFrom(l)
		lineages = append(lineages, ln)
		ids = append(ids, ln.ID)
	}
	var reps []quorum.Reputation
	if cards, err := q.s.store.q.ListQuorumReputations(ctx, db.ListQuorumReputationsParams{WorkspaceID: pgUUID(q.wsID), LineageIds: ids}); err == nil {
		for _, c := range cards {
			var r quorum.Reputation
			if json.Unmarshal(c, &r) == nil && r.Lineage != "" {
				reps = append(reps, r)
			}
		}
	}
	goal := quorum.Goal{Statement: strings.TrimSpace(q.obj.Prompt), Domain: "software", Deliverable: quorum.Deliverable{Kind: quorum.DeliverableCode}}
	if d, ok := cfg["deliverable"].(map[string]any); ok {
		kind, _ := d["kind"].(string)
		ac, _ := d["ac"].(string)
		format, _ := d["format"].(string)
		if kind = strings.TrimSpace(kind); kind == "" {
			kind = quorum.DeliverableCode
		}
		if !quorum.ValidDeliverableKind(kind) {
			return quorum.NewRunInput{}, fmt.Errorf("quorum.deliverable.kind %q: want answer, table, artifact or code", kind)
		}
		goal.Deliverable = quorum.Deliverable{Kind: kind, AC: strings.TrimSpace(ac), Format: strings.TrimSpace(format)}
	}
	if raw, ok := cfg["acceptance_criteria"].([]any); ok {
		for _, x := range raw {
			switch v := x.(type) {
			case string:
				if strings.TrimSpace(v) != "" {
					goal.ACs = append(goal.ACs, quorum.AC{Text: strings.TrimSpace(v)})
				}
			case map[string]any:
				text, _ := v["text"].(string)
				check, _ := v["check"].(string)
				if strings.TrimSpace(text) != "" {
					goal.ACs = append(goal.ACs, quorum.AC{Text: strings.TrimSpace(text), Check: strings.TrimSpace(check)})
				}
			}
		}
	}
	if att := attachmentLines(q.obj.Config); len(att) > 0 {
		goal.Statement += "\n\nAttachments:\n" + strings.Join(att, "\n")
	}
	plan, err := ParseQuorumPlan(cfg)
	if err != nil {
		return quorum.NewRunInput{}, err
	}
	return quorum.NewRunInput{RunID: q.runID, Goal: goal, Charter: ch, Lineages: lineages, Reputation: reps, BaseVersion: head, BaseBranch: base, Plan: plan}, nil
}

// ParseQuorumPlan reads config.quorum.plan, the bounties Scrybe hands a run
// in place of its planner, and validates it (quorum.ValidatePlan). No plan is
// nil.
func ParseQuorumPlan(cfg map[string]any) ([]quorum.BountyInput, error) {
	raw, ok := cfg["plan"]
	if !ok || raw == nil {
		return nil, nil
	}
	b, err := json.Marshal(raw)
	if err != nil {
		return nil, fmt.Errorf("quorum.plan: %w", err)
	}
	var plan []quorum.BountyInput
	if err := json.Unmarshal(b, &plan); err != nil {
		return nil, fmt.Errorf("quorum.plan must be a list of bounties: %w", err)
	}
	if err := quorum.ValidatePlan(plan); err != nil {
		return nil, fmt.Errorf("quorum.plan: %w", err)
	}
	return plan, nil
}

// qFamily maps a model id to its provider family.
func qFamily(model string) string {
	m := strings.ToLower(strings.TrimPrefix(strings.TrimPrefix(model, "anthropic/"), "openrouter/"))
	switch {
	case strings.HasPrefix(m, "claude") || strings.HasPrefix(m, "anthropic"):
		return "anthropic"
	case strings.Contains(m, "/"):
		return strings.SplitN(m, "/", 2)[0]
	case strings.HasPrefix(m, "gpt") || strings.HasPrefix(m, "o1") || strings.HasPrefix(m, "o3"):
		return "openai"
	}
	return "other"
}

// persist writes each kernel event before the decision is visible, and
// mirrors titled decisions into the run's activity stream.
func (q *qRun) persist(e quorum.Event) error {
	body, err := json.Marshal(e)
	if err != nil {
		return err
	}
	ctx := context.Background()
	if err := q.s.store.q.InsertQuorumEvent(ctx, db.InsertQuorumEventParams{WorkspaceID: pgUUID(q.wsID), RunID: pgUUID(q.runID), Seq: e.Seq, Type: e.Type, Hash: e.Hash, Body: string(body)}); err != nil {
		return err
	}
	if e.Payload.Title != "" && !qQuietEvents[e.Type] {
		level := "info"
		switch {
		case strings.HasPrefix(e.Type, "sanction"), strings.HasPrefix(e.Type, "governance"), e.Type == "taint.wave", e.Type == "claim.retracted", e.Type == "hitl.paged", e.Type == "intent.scope_denied":
			level = "warn"
		}
		go q.emit(ctx, Event{Type: "q." + e.Type, Level: level, Title: e.Payload.Title, Body: map[string]any{"seq": e.Seq, "actor": e.Actor}})
	}
	q.poke()
	return nil
}

var qQuietEvents = map[string]bool{"kernel.tick": true, "budget.spend": true, "lease.heartbeat": true, "lease.held": true, "adherence.sample": true, "reputation.calibrated": true, "bounty.critical_path": true, "assignment.started": true, "assignment.closed": true}

// --- loop ---

func (q *qRun) loop(ctx context.Context) {
	tick := time.NewTicker(3 * time.Second)
	defer tick.Stop()
	lastStatus := time.Time{}
	for {
		if ctx.Err() != nil {
			q.k.Abort(firstNonEmpty(q.stopMsg, "stopped by operator"))
			q.drainActions(context.WithoutCancel(ctx))
			q.stopAllSessions()
			return
		}
		q.k.Observe(q.host.SandboxSpendUSD(ctx, q.ideID, q.start))
		q.k.Tick()
		q.heartbeats()
		q.drainActions(ctx)
		if time.Since(lastStatus) > 10*time.Second {
			q.status(ctx)
			lastStatus = time.Now()
		}
		if q.k.Status() == quorum.RunStopped || q.k.Status() == quorum.RunAborted || q.k.Status() == quorum.RunFailed {
			q.drainActions(ctx)
			q.waitSessions(30 * time.Second)
			return
		}
		select {
		case <-ctx.Done():
		case <-tick.C:
		case <-q.wake:
			// Coalesce bursts of events into one pass.
			time.Sleep(200 * time.Millisecond)
		}
	}
}

func (q *qRun) heartbeats() {
	q.mu.Lock()
	ids := make([]string, 0, len(q.sess))
	for id := range q.sess {
		ids = append(ids, id)
	}
	q.mu.Unlock()
	for _, id := range ids {
		q.k.Heartbeat(id)
	}
}

// drainActions carries out the kernel's queued actions.
func (q *qRun) drainActions(ctx context.Context) {
	for _, a := range q.k.TakeActions() {
		a := a
		bg := context.WithoutCancel(ctx)
		switch a.Kind {
		case quorum.ActBidRound:
			go q.bidRound(bg, a)
		case quorum.ActStart:
			go q.startSession(ctx, a.Assignment)
		case quorum.ActStop:
			go q.stopSession(a.Assignment, a.Message)
		case quorum.ActSteer:
			q.notify(a.Assignment, a.Message)
		case quorum.ActGate:
			go q.runGate(bg, a.Claim, a.Assignment)
		case quorum.ActMerge:
			go q.merge(bg, a.Claim)
		case quorum.ActRevert:
			go q.revert(bg, a.Claim)
		case quorum.ActPage:
			q.emit(bg, Event{Type: "q.page", Level: "warn", Title: "Quorum needs a human: " + a.Message, Body: map[string]any{"item": a.Item}})
		case quorum.ActSideEffect:
			// No external connector executes in v0; the decision is recorded.
			q.k.RecordSideEffect(a.Item, "approved; no connector executes side effects in this version, so nothing was sent")
		case quorum.ActRecheck:
			go q.recheck(bg, a.Claim, a.Message)
		case quorum.ActRoute:
			for _, id := range a.Bounties {
				go q.route(bg, id)
			}
		case quorum.ActFinish:
			// finalize runs when the loop returns.
		}
	}
}

func (q *qRun) notify(asg, msg string) {
	q.mu.Lock()
	defer q.mu.Unlock()
	q.notices[asg] = append(q.notices[asg], msg)
}

// takeNotices returns and clears an assignment's kernel notices.
func (q *qRun) takeNotices(asg string) []string {
	q.mu.Lock()
	defer q.mu.Unlock()
	n := q.notices[asg]
	delete(q.notices, asg)
	return n
}

// --- bids ---

func (q *qRun) bidRound(ctx context.Context, a quorum.Action) {
	view := q.k.View()
	var lin quorum.Lineage
	for _, l := range view.Lineages {
		if l.ID == a.Lineage {
			lin = l.Lineage
		}
	}
	var briefs strings.Builder
	for _, id := range a.Bounties {
		for _, b := range view.Bounties {
			if b.ID == id {
				fmt.Fprintf(&briefs, "- id=%s kind=%s risk=%s value=%.2f claim_type=%s acs=%s files=%s\n  title: %s\n  instructions: %s\n", b.ID, b.Kind, b.Risk, b.Value, b.Spec.ClaimType, strings.Join(b.Spec.ACRefs, ","), strings.Join(b.Spec.Files, ","), b.Spec.Title, truncate(b.Spec.Instructions, 600))
			}
		}
	}
	bids, cost, proxy := q.askBids(ctx, lin, view.Goal, briefs.String())
	q.k.SubmitBids(a.Lineage, proxy, a.Bounties, bids, cost)
	q.poke()
}

const qBidSystem = `You speak for several workers in a software swarm bidding on open bounties. For each bounty you could do well, write up to %d bids, one per worker, that genuinely compete: different approaches (tools, order of work, how it is verified), not rewordings. The kernel scores every bid as value x confidence x diversity x standing / cost and the best one wins. Each bid gives a concrete approach (at most 3 sentences, specific about how), your honest probability of producing an admitted result (0..1, calibrated — overconfidence costs you reputation), your cost estimate in USD for your own model work (typically 0.1–1.5), wall-clock seconds (60–1500), and the repository files you expect to WRITE (paths relative to the repository root, or db://catalog.schema.table for a Databricks table you will write; only what you will change). Skip bounties you would do badly. Reply with JSON only: {"bids":[{"bounty_id":"...","approach":"...","confidence":0.0,"est_usd":0.0,"est_sec":0,"files":["..."]}]}`

const qBidTimeout = 25 * time.Second

// askBids asks the lineage's model for bids. Lineages the server model
// client cannot serve bid through the default model (marked proxy).
func (q *qRun) askBids(ctx context.Context, lin quorum.Lineage, goal, briefs string) ([]quorum.BidInput, float64, bool) {
	var fallback []quorum.BidInput
	for _, line := range strings.Split(briefs, "\n") {
		if id, ok := strings.CutPrefix(strings.TrimSpace(line), "- id="); ok {
			fallback = append(fallback, quorum.BidInput{Bounty: strings.Fields(id)[0], Approach: "Standard approach.", Confidence: 0.5, EstUSD: 0.5, EstSec: 600})
		}
	}
	if q.s.llm == nil || !q.s.llm.Enabled() {
		return fallback, 0, true
	}
	model, proxy := strings.TrimPrefix(lin.Model, "anthropic/"), false
	if lin.Family != "anthropic" {
		model, proxy = q.s.plannerModel(), true
	}
	persona := ""
	if lin.Persona != "" {
		persona = "\n\nYour persona:\n" + truncate(lin.Persona, 1500)
	}
	user := "Goal:\n" + truncate(goal, 3000) + persona + "\n\nOpen bounties:\n" + briefs
	// A bid is a small call on the critical path: a slow reply falls back to
	// the standard bid rather than holding the bounty.
	ctx, cancel := context.WithTimeout(ctx, qBidTimeout)
	defer cancel()
	system := fmt.Sprintf(qBidSystem, q.k.ProposalsPerBounty())
	text, cost, err := qChat(ctx, q.s, model, system, user)
	if err != nil && !proxy {
		// The lineage's own model is not served here; bid through the default.
		text, cost, err = qChat(ctx, q.s, q.s.plannerModel(), system, user)
		proxy = true
	}
	if err != nil {
		return fallback, cost, true
	}
	var out struct {
		Bids []quorum.BidInput `json:"bids"`
	}
	if err := json.Unmarshal([]byte(qJSONObject(text)), &out); err != nil {
		return fallback, cost, true
	}
	return out.Bids, cost, proxy
}

// qJSONObject extracts the outermost JSON object from model text.
func qJSONObject(s string) string {
	i, j := strings.Index(s, "{"), strings.LastIndex(s, "}")
	if i < 0 || j <= i {
		return "{}"
	}
	return s[i : j+1]
}

// --- sessions ---

func randomToken() string {
	b := make([]byte, 24)
	_, _ = rand.Read(b)
	return hex.EncodeToString(b)
}

func tokenHash(t string) string {
	s := sha256.Sum256([]byte(t))
	return hex.EncodeToString(s[:])
}

// qWorktreeScript prepares an assignment's worktree. $1 dir, $2 start ref,
// $3 branch ("" = detached).
const qWorktreeScript = `set -e
mkdir -p /home/ide/swarm
cd /home/ide/workspace
git worktree prune
rm -rf "$1"
if [ -n "$3" ]; then git worktree add -q -B "$3" "$1" "$2"; else git worktree add -q --detach "$1" "$2"; fi`

func (q *qRun) startSession(ctx context.Context, asgID string) {
	info, ok := q.k.Assignment(asgID)
	if !ok {
		return
	}
	a := info.Assignment
	dir := gsWorkersDir + "/" + a.ID
	start, branch := firstNonEmpty(a.BaseVersion, "HEAD"), ""
	switch a.Role {
	case quorum.RoleVerifier, quorum.RoleJuror:
		if t := info.Target; t != nil {
			for _, r := range t.Body.Artifacts {
				if r.Kind == "git" {
					start = r.Ref
				}
			}
		}
	case quorum.RoleDecomposer:
		if q.k.Charter().Market.FastDecompose && q.fastDecompose(ctx, info) {
			return
		}
		branch = "quorum/" + q.runShort() + "/" + a.ID
	default:
		branch = "quorum/" + q.runShort() + "/" + a.ID
	}
	// A sandbox that is still waking or busy fails transiently: retry before
	// charging the assignment with a failed attempt.
	var werr error
	for try := 0; try < 3; try++ {
		out, err := q.exec(ctx, qWorktreeScript, dir, start, branch)
		if err == nil {
			werr = nil
			break
		}
		werr = fmt.Errorf("%s %v", strings.TrimSpace(lastLineStr(out)), err)
		select {
		case <-ctx.Done():
			return
		case <-time.After(time.Duration(3*(try+1)) * time.Second):
		}
	}
	if werr != nil {
		q.k.SessionEnded(asgID, true, "worktree: "+truncate(werr.Error(), 300))
		return
	}
	token := randomToken()
	q.mu.Lock()
	q.tokens[tokenHash(token)] = asgID
	q.mu.Unlock()
	name := firstNonEmpty(info.Lineage.Label, info.Lineage.Model)
	env := map[string]string{
		"Q_RUN_ID": q.runID, "Q_ASSIGNMENT": a.ID, "Q_TOKEN": token, "Q_ROLE": a.Role,
		"GIT_AUTHOR_NAME": a.Agent + " (" + name + ")", "GIT_AUTHOR_EMAIL": a.Agent + "@quorum.local",
		"GIT_COMMITTER_NAME": a.Agent + " (" + name + ")", "GIT_COMMITTER_EMAIL": a.Agent + "@quorum.local",
	}
	tools := ""
	if len(q.g.credEnv) > 0 {
		tools = q.g.toolCatalog()
	}
	prompt := qPrompt(info, q.k.ContextSlice(asgID), dir, tools)
	title := fmt.Sprintf("Quorum %s · %s · %s", a.ID, a.Role, name)
	sessionID, msgID, err := q.host.StartWorkerSession(ctx, q.ideID, dir, title, info.Lineage.Model, env, prompt)
	if err != nil {
		q.dropToken(asgID)
		q.k.SessionEnded(asgID, true, "session did not start: "+err.Error())
		return
	}
	ss := &qSess{asg: asgID, sessionID: sessionID, dir: dir, token: token, started: time.Now()}
	q.mu.Lock()
	q.sess[asgID] = ss
	q.mu.Unlock()
	q.k.SessionStarted(asgID)
	q.emit(ctx, Event{Type: "q.session", Title: fmt.Sprintf("%s (%s) started %s", a.Agent, a.Role, info.Bounty.Spec.Title), Body: map[string]any{"assignment": a.ID, "session_id": sessionID, "agent": a.Agent}})
	go q.waitSession(ctx, ss, msgID, info)
}

// waitSession waits for the session; a builder that ends without proposing
// gets one nudge.
func (q *qRun) waitSession(ctx context.Context, ss *qSess, msgID string, info quorum.AssignmentInfo) {
	limit := time.Duration(info.Charter.Run.AssignmentSec) * time.Second
	wctx, cancel := context.WithTimeout(ctx, limit)
	final, outcome, err := q.host.WaitWorker(wctx, q.ideID, ss.sessionID, msgID)
	timedOut := wctx.Err() != nil && ctx.Err() == nil
	cancel()
	if timedOut {
		q.host.InterruptWorker(context.Background(), q.ideID, ss.sessionID)
	}
	bg := context.WithoutCancel(ctx)
	cur, _ := q.k.Assignment(ss.asg)
	if cur.Assignment.Status == quorum.AsgActive && !timedOut && err == nil && ctx.Err() == nil && !ss.nudged {
		if nudge := qNudge(cur); nudge != "" {
			ss.nudged = true
			if mid, perr := q.host.PromptWorker(bg, q.ideID, ss.sessionID, nudge); perr == nil {
				rctx, rcancel := context.WithTimeout(ctx, 6*time.Minute)
				final, outcome, err = q.host.WaitWorker(rctx, q.ideID, ss.sessionID, mid)
				if rctx.Err() != nil {
					q.host.InterruptWorker(context.Background(), q.ideID, ss.sessionID)
				}
				rcancel()
			}
		}
	}
	reason := "session finished"
	failed := false
	switch {
	case timedOut:
		reason = "the assignment ran past its time limit"
	case err != nil:
		reason, failed = "session error: "+err.Error(), true
	case outcome == "failed":
		reason, failed = "the session failed", true
	default:
		if b := qBlocked(final); b != "" {
			reason = b
		}
	}
	q.dropToken(ss.asg)
	q.mu.Lock()
	delete(q.sess, ss.asg)
	q.mu.Unlock()
	q.k.SessionEnded(ss.asg, failed, reason)
	if info.Assignment.Role == quorum.RoleVerifier || info.Assignment.Role == quorum.RoleJuror {
		_, _ = q.exec(bg, `cd /home/ide/workspace && git worktree remove --force "$1" >/dev/null 2>&1; true`, ss.dir)
	}
	q.poke()
}

// qBlocked is the agent's own account of why it could not do its bounty,
// from a final message line starting "BLOCKED:".
func qBlocked(final string) string {
	for _, line := range strings.Split(final, "\n") {
		line = strings.TrimLeft(strings.TrimSpace(line), "*_` ")
		if rest, ok := strings.CutPrefix(line, "BLOCKED:"); ok && strings.TrimSpace(rest) != "" {
			return "blocked: " + truncate(strings.Trim(strings.TrimSpace(rest), "*_` "), 300)
		}
	}
	return ""
}

func (q *qRun) dropToken(asg string) {
	q.mu.Lock()
	defer q.mu.Unlock()
	for h, a := range q.tokens {
		if a == asg {
			delete(q.tokens, h)
		}
	}
}

func (q *qRun) stopSession(asg, why string) {
	q.mu.Lock()
	ss := q.sess[asg]
	q.mu.Unlock()
	q.dropToken(asg)
	if ss == nil {
		return
	}
	q.host.InterruptWorker(context.Background(), q.ideID, ss.sessionID)
}

func (q *qRun) stopAllSessions() {
	q.mu.Lock()
	var ids []string
	for id := range q.sess {
		ids = append(ids, id)
	}
	q.mu.Unlock()
	for _, id := range ids {
		q.stopSession(id, "run stopped")
	}
	q.waitSessions(20 * time.Second)
}

func (q *qRun) waitSessions(d time.Duration) {
	deadline := time.Now().Add(d)
	for time.Now().Before(deadline) {
		q.mu.Lock()
		n := len(q.sess)
		q.mu.Unlock()
		if n == 0 {
			return
		}
		time.Sleep(500 * time.Millisecond)
	}
	q.stopAllSessionsNoWait()
}

func (q *qRun) stopAllSessionsNoWait() {
	q.mu.Lock()
	defer q.mu.Unlock()
	for _, ss := range q.sess {
		q.host.InterruptWorker(context.Background(), q.ideID, ss.sessionID)
	}
}

// --- deterministic gates ---

// qTestPattern matches test files (the pinned test set).
const qTestPattern = `(^|/)(tests?|__tests__|spec)/|(^|/)test_[^/]*$|_test\.[a-z]+$|\.test\.[a-z]+$|\.spec\.[a-z]+$|(^|/)conftest\.py$`

// qGateScript runs the code gate in a fresh worktree at the claim commit.
// $1 dir, $2 commit, $3 base, $4 timeout s, $5 test command ("" = detect),
// $6 lint command, then AC checks as "id=cmd" pairs. Prints sections.
const qGateScript = `cd /home/ide/workspace || exit 1
git worktree prune
rm -rf "$1"
git worktree add -q --detach "$1" "$2" >/dev/null 2>&1 || { echo "FATAL could not check out $2"; exit 0; }
cd "$1"
if [ -n "$3" ] && ! git merge-base --is-ancestor "$3" "$2" 2>/dev/null; then echo "FATAL the claim commit is not based on its base version $(echo $3 | cut -c1-10)"; fi
echo "CHANGED $(git diff --name-only "$3" "$2" | tr '\n' ' ')"
to=$4; tests=$5; lint=$6; shift 6
if [ -z "$tests" ]; then
  if [ -f Makefile ] && grep -qE '^test:' Makefile; then tests="make test"
  elif [ -f package.json ] && grep -q '"test"' package.json; then tests="npm test --silent"
  elif [ -f go.mod ]; then tests="go test ./..."
  elif [ -f pytest.ini ] || [ -f pyproject.toml ] || [ -f setup.cfg ] || [ -d tests ] || ls test_*.py >/dev/null 2>&1; then
    if python3 -c 'import pytest' >/dev/null 2>&1; then tests="python3 -m pytest -q"
    elif [ -d tests ]; then tests="python3 -m unittest discover -s tests -q"
    else tests="python3 -m unittest discover -q"
    fi
  fi
fi
run() { # name cmd
  out=$(timeout "$to" sh -c "$2" 2>&1); code=$?
  echo "CHECK $1 $code $(printf '%s' "$2" | base64 | tr -d '\n') $(printf '%s' "$out" | tail -c 2500 | base64 | tr -d '\n')"
}
[ -n "$tests" ] && run tests "$tests"
[ -n "$lint" ] && run lint "$lint"
for pair in "$@"; do run "${pair%%=*}" "${pair#*=}"; done
echo "ADDED $(git diff "$3" "$2" | grep '^+' | grep -v '^+++' | head -c 20000 | base64 | tr -d '\n')"
cd /home/ide/workspace && git worktree remove --force "$1" >/dev/null 2>&1
true`

var qTestRe = mustRegexp(qTestPattern)

func (q *qRun) runGate(ctx context.Context, cid, asg string) {
	q.mu.Lock()
	if q.busy[cid] {
		q.mu.Unlock()
		return
	}
	q.busy[cid] = true
	q.mu.Unlock()
	defer func() {
		q.mu.Lock()
		delete(q.busy, cid)
		q.mu.Unlock()
		q.poke()
	}()
	c, ok := q.k.Claim(cid)
	if !ok {
		return
	}
	switch c.Body.Type {
	case quorum.ClaimCode:
		q.k.RecordGate(cid, q.codeGate(ctx, c))
	case quorum.ClaimFact:
		q.k.RecordGate(cid, q.factGate(ctx, c))
	case quorum.ClaimPlan:
		view := q.k.View()
		children := 0
		for _, b := range view.Bounties {
			if b.Parent == c.Body.Bounty && b.Kind != quorum.KindVerify {
				children++
			}
		}
		if children == 0 {
			q.k.RecordGate(cid, quorum.GateReport{Pass: false, Reason: "a plan claim needs the child bounties it describes; post them with q_post first"})
			return
		}
		q.k.RecordGate(cid, quorum.GateReport{Pass: true, Strong: true, Reason: fmt.Sprintf("%d child bounties posted", children), Checks: []quorum.Evidence{{Kind: "checker", Summary: fmt.Sprintf("decomposition posted %d child bounties", children)}}})
	default:
		if r, ok := q.answerGate(ctx, c); ok {
			q.k.RecordGate(cid, r)
			return
		}
		q.k.RecordGate(cid, quorum.GateReport{Pass: true, Reason: "schema check passed; " + c.Body.Type + " claims are admitted by independent verdicts", Checks: []quorum.Evidence{{Kind: "checker", Summary: "statement present; dependencies admitted"}}})
	}
}

// qAnswerScript runs AC checks against a candidate answer in the integration
// worktree (the repository before there is one). $1 timeout, $2 the answer,
// then id=check pairs; each check reads the answer from $QUORUM_ANSWER and
// reports a CHECK line as qGateScript does.
const qAnswerScript = `cd /home/ide/swarm/q-integration 2>/dev/null || cd /home/ide/workspace 2>/dev/null || true
to=$1; QUORUM_ANSWER=$2; export QUORUM_ANSWER; shift 2
run() { # name cmd
  out=$(timeout "$to" sh -c "$2" 2>&1); code=$?
  echo "CHECK $1 $code $(printf '%s' "$2" | base64 | tr -d '\n') $(printf '%s' "$out" | tail -c 2500 | base64 | tr -d '\n')"
}
for pair in "$@"; do run "${pair%%=*}" "${pair#*=}"; done
true`

// answerGate checks the claim that answers a non-code deliverable: when the
// claim can be the deliverable (quorum.Kernel.CanDeliver) and the deliverable
// AC has a check, every check of the ACs it cites runs with the claim's
// statement as $QUORUM_ANSWER, and the gate is strong. ok is false for any
// other claim, slices of the answer included.
func (q *qRun) answerGate(ctx context.Context, c quorum.Claim) (quorum.GateReport, bool) {
	d := q.k.Deliverable()
	if !q.k.CanDeliver(c.CID) {
		return quorum.GateReport{}, false
	}
	var checks []quorum.ACView
	checked := false
	for _, ac := range q.k.View().ACs {
		if ac.Check != "" && slices.Contains(c.Body.ACRefs, ac.ID) {
			checks = append(checks, ac)
			checked = checked || ac.ID == d.AC
		}
	}
	if !checked {
		return quorum.GateReport{}, false
	}
	timeout := max(60, q.k.Charter().Verification.Gates.CodeChange.TimeoutSec)
	args := []string{strconv.Itoa(timeout), c.Body.Statement}
	var toolChecks []quorum.ACView
	for _, ac := range checks {
		if strings.HasPrefix(ac.Check, qToolCheckPrefix) {
			toolChecks = append(toolChecks, ac)
		} else {
			args = append(args, ac.ID+"="+ac.Check)
		}
	}
	gctx, cancel := context.WithTimeout(ctx, time.Duration(timeout*(1+len(checks)))*time.Second)
	defer cancel()
	r := quorum.GateReport{Pass: true, Strong: true}
	var failed []string
	if len(args) > 2 {
		// Checks read the integration worktree, which merges change.
		q.mergeMu.Lock()
		out, err := q.exec(gctx, qAnswerScript, args...)
		q.mergeMu.Unlock()
		if err != nil && out == "" {
			return quorum.GateReport{Pass: false, Reason: "the gate could not run: " + err.Error()}, true
		}
		for _, line := range strings.Split(out, "\n") {
			if name, ok, ev, valid := qCheckLine(line); valid {
				if !ok {
					failed = append(failed, name)
				}
				r.Checks = append(r.Checks, ev)
			}
		}
	}
	for _, ac := range toolChecks {
		ok, ev := q.toolCheck(gctx, ac)
		if !ok {
			failed = append(failed, ac.ID)
		}
		r.Checks = append(r.Checks, ev)
	}
	switch {
	case len(failed) > 0:
		r.Pass, r.Reason, r.FailedACs, r.ACOnly = false, "failed: "+strings.Join(failed, ", "), failed, true
	case len(r.Checks) < len(checks):
		// A check did not report: no strong pass without it.
		r.Strong, r.Reason = false, "not every AC check reported; verification decides"
	default:
		r.Reason = fmt.Sprintf("%d check(s) passed on the answer", len(r.Checks))
	}
	return r, true
}

// qCheckLine parses a gate script's CHECK line into the check's name, its
// outcome and its evidence; valid is false for any other line.
func qCheckLine(line string) (name string, ok bool, ev quorum.Evidence, valid bool) {
	if !strings.HasPrefix(line, "CHECK ") {
		return "", false, quorum.Evidence{}, false
	}
	parts := strings.SplitN(line, " ", 5)
	if len(parts) < 4 {
		return "", false, quorum.Evidence{}, false
	}
	name, code := parts[1], parts[2]
	cmd := b64(parts[3])
	tail := ""
	if len(parts) == 5 {
		tail = b64(parts[4])
	}
	ok = code == "0"
	verdict := "passed"
	if !ok {
		verdict = "failed (exit " + code + ")"
	}
	ev = quorum.Evidence{Kind: "test_run", Ref: cmd, Summary: fmt.Sprintf("%s `%s` %s: %s", name, truncate(cmd, 200), verdict, truncate(lastLines(tail, 6), 900)), SHA256: tokenHash(tail)}
	return name, ok, ev, true
}

func (q *qRun) codeGate(ctx context.Context, c quorum.Claim) quorum.GateReport {
	commit := ""
	for _, r := range c.Body.Artifacts {
		if r.Kind == "git" {
			commit = r.Ref
		}
	}
	view := q.k.View()
	cg := q.k.Charter().Verification.Gates.CodeChange
	timeout := max(60, cg.TimeoutSec)
	args := []string{gsWorkersDir + "/gate-" + c.CID[:10], commit, c.Body.BaseVersion, strconv.Itoa(timeout), cg.Tests, cg.Lint}
	var toolChecks []quorum.ACView
	for _, id := range c.Body.ACRefs {
		for _, ac := range view.ACs {
			if ac.ID != id || ac.Check == "" {
				continue
			}
			if strings.HasPrefix(ac.Check, qToolCheckPrefix) {
				toolChecks = append(toolChecks, ac)
			} else {
				args = append(args, ac.ID+"="+ac.Check)
			}
		}
	}
	gctx, cancel := context.WithTimeout(ctx, time.Duration(timeout*(2+len(args)))*time.Second)
	defer cancel()
	out, err := q.exec(gctx, qGateScript, args...)
	if err != nil && out == "" {
		return quorum.GateReport{Pass: false, Reason: "the gate could not run: " + err.Error()}
	}
	r := quorum.GateReport{Pass: true, Strong: true}
	ran := 0
	var failed []string
	for _, line := range strings.Split(out, "\n") {
		switch {
		case strings.HasPrefix(line, "FATAL "):
			r.Pass = false
			r.Reason = strings.TrimPrefix(line, "FATAL ")
		case strings.HasPrefix(line, "CHANGED "):
			for _, f := range strings.Fields(strings.TrimPrefix(line, "CHANGED ")) {
				if qTestRe.MatchString(f) {
					r.TestEdits = append(r.TestEdits, f)
				}
			}
		case strings.HasPrefix(line, "CHECK "):
			name, ok, ev, valid := qCheckLine(line)
			if !valid {
				continue
			}
			ran++
			if !ok {
				failed = append(failed, name)
			}
			r.Checks = append(r.Checks, ev)
		case strings.HasPrefix(line, "ADDED "):
			r.ArtifactText = b64(strings.TrimPrefix(line, "ADDED "))
		}
	}
	if r.Reason != "" {
		return r
	}
	for _, ac := range toolChecks {
		ran++
		ok, ev := q.toolCheck(gctx, ac)
		if !ok {
			failed = append(failed, ac.ID)
		}
		r.Checks = append(r.Checks, ev)
	}
	switch {
	case len(failed) > 0:
		r.Pass, r.Reason = false, "failed: "+strings.Join(failed, ", ")
		for _, f := range failed {
			if strings.HasPrefix(f, "AC-") {
				r.FailedACs = append(r.FailedACs, f)
			}
		}
		r.ACOnly = len(r.FailedACs) == len(failed)
	case ran == 0:
		// Nothing to run: the gate passes through and verification decides.
		r.Strong, r.Reason = false, "no test command or AC check found; verification decides"
	default:
		r.Reason = fmt.Sprintf("%d check(s) passed in a fresh worktree at the claim commit", ran)
	}
	return r
}

// qRecheckScript runs one AC check in the integration worktree. $1 timeout,
// $2 the check.
const qRecheckScript = `cd /home/ide/swarm/q-integration || exit 97
out=$(timeout "$1" sh -c "$2" 2>&1); code=$?
printf '%s\n' "$out" | tail -c 2500
exit $code`

// recheck re-runs a deferred AC's check on the merged integration head and
// reports it to the kernel.
func (q *qRun) recheck(ctx context.Context, cid, acID string) {
	var ac quorum.ACView
	for _, a := range q.k.View().ACs {
		if a.ID == acID {
			ac = a
		}
	}
	if ac.Check == "" {
		return
	}
	if strings.HasPrefix(ac.Check, qToolCheckPrefix) {
		ok, ev := q.toolCheck(ctx, ac)
		q.k.SettleDeferred(cid, acID, ok, ev)
		q.poke()
		return
	}
	q.mergeMu.Lock()
	out, err := q.exec(ctx, qRecheckScript, "600", ac.Check)
	q.mergeMu.Unlock()
	verdict := "passed"
	if err != nil {
		verdict = "failed"
	}
	ev := quorum.Evidence{Kind: "test_run", Ref: ac.Check, Summary: fmt.Sprintf("%s `%s` %s: %s", acID, truncate(ac.Check, 200), verdict, truncate(lastLines(out, 6), 900)), SHA256: tokenHash(out)}
	q.k.SettleDeferred(cid, acID, err == nil, ev)
	q.poke()
}

// qToolCheckPrefix marks an AC check that runs on the server as a read-only
// capability call — `tool:<capability> <tool> <json>` — for state the sandbox
// cannot reach (a Databricks target). It passes only when the output carries
// RESULT: PASS. Like every gate, it is deterministic and never asks a model.
const qToolCheckPrefix = "tool:"

func (q *qRun) toolCheck(ctx context.Context, ac quorum.ACView) (bool, quorum.Evidence) {
	spec := strings.TrimSpace(strings.TrimPrefix(ac.Check, qToolCheckPrefix))
	fail := func(msg string) (bool, quorum.Evidence) {
		return false, quorum.Evidence{Kind: "test_run", Ref: ac.Check, Summary: fmt.Sprintf("%s `%s` failed: %s", ac.ID, truncate(ac.Check, 200), truncate(msg, 900)), SHA256: tokenHash(msg)}
	}
	capName, rest, _ := strings.Cut(spec, " ")
	name, args, _ := strings.Cut(strings.TrimSpace(rest), " ")
	args = strings.TrimSpace(args)
	if args == "" {
		args = "{}"
	}
	c, ok := q.s.reg.Get(capName)
	if !ok || !json.Valid([]byte(args)) {
		return fail("the check must be tool:<capability> <tool> <json>")
	}
	rt, ok := c.(qResourceTool)
	if !ok || !rt.IsReadOnlyTool(name, json.RawMessage(args)) {
		return fail("a tool check may only call a read-only tool")
	}
	sb, err := q.g.capSandbox(ctx, c)
	if err != nil {
		return fail("could not start the " + capName + " sandbox: " + err.Error())
	}
	out := invokeCap(ctx, c, sb, name, json.RawMessage(args)).Text
	if !strings.Contains(out, "RESULT: PASS") {
		return fail(lastLines(out, 8))
	}
	return true, quorum.Evidence{Kind: "test_run", Ref: ac.Check, Summary: fmt.Sprintf("%s `%s` passed: %s", ac.ID, truncate(ac.Check, 200), truncate(lastLines(out, 8), 900)), SHA256: tokenHash(out)}
}

func b64(s string) string {
	b, err := decodeB64(strings.TrimSpace(s))
	if err != nil {
		return ""
	}
	return string(b)
}

func lastLines(s string, n int) string {
	lines := strings.Split(strings.TrimRight(s, "\n"), "\n")
	if len(lines) > n {
		lines = lines[len(lines)-n:]
	}
	return strings.Join(lines, "\n")
}

// factGate: ≥2 fetched sources whose quotes appear in the stored blobs and
// carry the statement's numbers (§7.3.4).
func (q *qRun) factGate(ctx context.Context, c quorum.Claim) quorum.GateReport {
	norm := func(s string) string { return strings.ToLower(strings.Join(strings.Fields(s), " ")) }
	var quotes []string
	var checks []quorum.Evidence
	for _, e := range c.Body.Evidence {
		if e.Kind != "source" || e.Ref == "" || strings.TrimSpace(e.Quote) == "" {
			continue
		}
		content, err := q.s.store.q.GetQuorumBlob(ctx, db.GetQuorumBlobParams{RunID: pgUUID(q.runID), Sha256: e.Ref})
		if err != nil {
			return quorum.GateReport{Pass: false, Reason: "source " + truncate(e.Ref, 12) + " was not fetched with q_fetch"}
		}
		if !strings.Contains(norm(content), norm(e.Quote)) {
			return quorum.GateReport{Pass: false, Reason: "the quote attributed to " + truncate(e.Ref, 12) + " does not appear in the fetched source"}
		}
		quotes = append(quotes, e.Quote)
		checks = append(checks, quorum.Evidence{Kind: "source", Ref: e.Ref, Summary: "quote found in fetched source", Quote: truncate(e.Quote, 400)})
	}
	if len(quotes) < 2 {
		return quorum.GateReport{Pass: false, Reason: fmt.Sprintf("a fact needs at least 2 fetched sources with quotes (found %d)", len(quotes))}
	}
	all := strings.Join(quotes, "\n")
	for _, n := range qNumbers(c.Body.Statement) {
		if !strings.Contains(all, n) {
			return quorum.GateReport{Pass: false, Reason: "the number " + n + " in the statement is not in any quoted source"}
		}
	}
	return quorum.GateReport{Pass: true, Strong: true, Reason: fmt.Sprintf("%d sources contain the quoted spans", len(quotes)), Checks: checks}
}

// --- integration ---

// qMergeScript merges a claim commit into the integration worktree. $1
// commit, $2 message. Prints MERGED <sha> | ALREADY <sha> | CONFLICT <files>.
const qMergeScript = `cd /home/ide/swarm/q-integration || exit 1
if git merge-base --is-ancestor "$1" HEAD; then echo "ALREADY $(git rev-parse HEAD)"; exit 0; fi
if git -c user.name="Quorum" -c user.email="quorum@alpha-sygnal.local" merge -q --no-ff --no-edit -m "$2" "$1" >/dev/null 2>&1; then
  echo "MERGED $(git rev-parse HEAD)"
else
  files=$(git diff --name-only --diff-filter=U | tr '\n' ' ')
  git merge --abort >/dev/null 2>&1
  echo "CONFLICT $files"
fi`

func (q *qRun) merge(ctx context.Context, cid string) {
	c, ok := q.k.Claim(cid)
	if !ok {
		return
	}
	commit := ""
	for _, r := range c.Body.Artifacts {
		if r.Kind == "git" {
			commit = r.Ref
		}
	}
	q.mergeMu.Lock()
	msg := fmt.Sprintf("[quorum|claim=%s] %s", cid[:12], truncate(oneLineStr(c.Body.Statement), 160))
	out, err := q.exec(ctx, qMergeScript, commit, msg)
	q.mergeMu.Unlock()
	line := strings.TrimSpace(lastLineStr(out))
	verb, rest, _ := strings.Cut(line, " ")
	switch {
	case err == nil && (verb == "MERGED" || verb == "ALREADY") && len(rest) == 40:
		q.k.RecordMerge(cid, rest, false, "")
		branch := "quorum/" + q.runShort() + "/" + q.assignmentOf(cid)
		go q.g.pushRefs(context.WithoutCancel(ctx), []GsPushRef{{Branch: q.integrationBranch(), SHA: rest}, {Branch: branch, SHA: commit, Force: true}}, nil)
	default:
		q.k.RecordMerge(cid, "", true, firstNonEmpty(strings.TrimPrefix(line, "CONFLICT "), errString(err)))
	}
	q.poke()
}

func (q *qRun) assignmentOf(cid string) string {
	v := q.k.View()
	for _, a := range v.Assignments {
		for _, x := range a.Claims {
			if x == cid {
				return a.ID
			}
		}
	}
	return "claim-" + cid[:8]
}

// revert undoes a retracted claim's merge on the integration branch.
func (q *qRun) revert(ctx context.Context, cid string) {
	c, ok := q.k.Claim(cid)
	if !ok || c.MergeSHA == "" {
		return
	}
	q.mergeMu.Lock()
	out, err := q.exec(ctx, `cd /home/ide/swarm/q-integration || exit 1
if git -c user.name="Quorum" -c user.email="quorum@alpha-sygnal.local" revert --no-edit -m 1 "$1" >/dev/null 2>&1; then echo "REVERTED $(git rev-parse HEAD)"; else git revert --abort >/dev/null 2>&1; echo "FAILED"; fi`, c.MergeSHA)
	q.mergeMu.Unlock()
	line := strings.TrimSpace(lastLineStr(out))
	if v, ok := strings.CutPrefix(line, "REVERTED "); ok && err == nil {
		q.emit(ctx, Event{Type: "q.revert", Level: "warn", Title: "reverted retracted claim " + cid[:10] + " on the integration branch"})
		go q.g.pushRefs(context.WithoutCancel(ctx), []GsPushRef{{Branch: q.integrationBranch(), SHA: v}}, nil)
	} else {
		q.emit(ctx, Event{Type: "q.revert", Level: "error", Title: "could not revert retracted claim " + cid[:10] + "; the integration branch still contains it"})
	}
}

// --- status and finalize ---

func (q *qRun) status(ctx context.Context) {
	v := q.k.View()
	switch v.Run.Status {
	case quorum.RunConverging:
		q.run.Status = RunConverging
	default:
		q.run.Status = RunBuilding
		if v.Metrics.Coverage == 0 {
			q.run.Status = RunExploring
		}
	}
	q.run.Progress = clamp01(v.Metrics.Coverage)
	admitted := 0
	for _, c := range v.Claims {
		if c.Status == quorum.ClaimAdmitted {
			admitted++
		}
	}
	q.run.Confidence = clamp01(v.Metrics.RequiredCoverage)
	q.run.VerdictSentence = fmt.Sprintf("%s · coverage %.0f%% · %d active · %d claims admitted · $%.2f of $%.2f", v.Run.Status, v.Metrics.Coverage*100, v.Metrics.Active, admitted, v.Metrics.SpendUSD, v.Metrics.BudgetUSD)
	_ = q.s.store.UpdateRunSwarm(ctx, q.run)
	q.emit(ctx, Event{Type: EvSpend, Body: map[string]any{"heartbeat": true, "spend_usd": round2(v.Metrics.SpendUSD)}})
}

// qFinalMergeScript merges the integration branch into the base branch. The
// main workspace is detached first: git will not add a worktree for a branch
// that is checked out elsewhere.
var qFinalMergeScript = "cd /home/ide/workspace && git checkout -q --detach >/dev/null 2>&1\n" +
	strings.NewReplacer(`user.name="GitSwarm"`, `user.name="Quorum"`, "gitswarm@alpha-sygnal.local", "quorum@alpha-sygnal.local").Replace(gsFinalMergeScript)

// publishFinal pushes the integration branch and, for a run that stopped
// ready, merges it into the base branch and pushes that (never forced).
func (q *qRun) publishFinal(ctx context.Context, v quorum.View) (bool, string) {
	head := v.Run.IntegrationHead
	pushed := q.g.pushRefs(ctx, []GsPushRef{{Branch: q.integrationBranch(), SHA: head}}, nil)
	ready := v.Run.Status == quorum.RunStopped && v.Run.StoppedReason == "ready"
	if !ready || !gsMergeSolutionEnabled(q.obj.Config) || q.g.baseBranch == "" || head == "" {
		return pushed, ""
	}
	msg := fmt.Sprintf("Merge Quorum integration %s (run %s, all acceptance criteria admitted)", short(head), q.runShort())
	q.mergeMu.Lock()
	out, err := q.exec(ctx, qFinalMergeScript, gsWorkersDir+"/q-final-merge", q.g.baseBranch, head, msg)
	q.mergeMu.Unlock()
	line := strings.TrimSpace(lastLineStr(out))
	verb, rest, _ := strings.Cut(line, " ")
	merged := ""
	if err == nil && (verb == "MERGED" || verb == "UPTODATE") && len(rest) == 40 {
		ok := q.g.pushRefs(ctx, []GsPushRef{{Branch: q.g.baseBranch, SHA: rest}}, nil)
		merged = fmt.Sprintf("The integration branch is merged into %s", q.g.baseBranch)
		if !ok && q.g.hasRemote(ctx) {
			merged += " in the sandbox repository (the push to GitHub failed)"
		}
		merged += "."
	} else {
		merged = fmt.Sprintf("The integration branch conflicts with %s (%s); merge %s by hand.", q.g.baseBranch, truncate(strings.TrimSpace(strings.TrimPrefix(line, "CONFLICT")), 200), q.integrationBranch())
	}
	q.k.SetFinalMerge(merged)
	return pushed, merged
}

// QuorumPublishResult is the outcome of a publish pass.
type QuorumPublishResult struct {
	Pushed bool   `json:"pushed"`
	Merged string `json:"merged"`
}

// PublishQuorum pushes a finished run's integration branch and, if the run
// stopped ready, merges it into the base branch. It is safe to repeat.
func (s *Service) PublishQuorum(ctx context.Context, wsID, runID string) (QuorumPublishResult, error) {
	if s.qLiveRun(runID) != nil {
		return QuorumPublishResult{}, qErr(409, "the run is still working; it publishes itself when it stops")
	}
	if s.gs == nil {
		return QuorumPublishResult{}, qErr(409, "Quorum needs the Alpha Sygnal IDE")
	}
	run, err := s.store.GetRun(ctx, wsID, runID)
	if err != nil {
		return QuorumPublishResult{}, err
	}
	obj, err := s.store.GetObjective(ctx, wsID, run.ObjectiveID)
	if err != nil {
		return QuorumPublishResult{}, err
	}
	sink := NewEventSink(s.store, s.cast)
	q := &qRun{s: s, host: s.gs, wsID: wsID, runID: runID, obj: obj, run: run, sink: sink, start: time.Now(),
		sess: map[string]*qSess{}, tokens: map[string]string{}, notices: map[string][]string{}, busy: map[string]bool{}, wake: make(chan struct{}, 1)}
	q.g = &gsRun{s: s, host: s.gs, wsID: wsID, runID: runID, obj: obj, run: run, sink: sink, start: q.start,
		live: map[string]*gsLive{}, published: map[string]string{}, unmerged: map[string]string{}, capSB: map[string]Sandbox{}}
	q.g.owner, _ = obj.Config["created_by"].(string)
	q.g.repoURL = objectiveRepoURL(obj.Config)
	squadID, _ := obj.Config["squad_id"].(string)
	ideID, err := s.gs.EnsureGitSwarmSandbox(ctx, wsID, runID, q.g.repoURL, squadID, q.g.owner)
	if err != nil {
		return QuorumPublishResult{}, err
	}
	q.ideID, q.g.ideID = ideID, ideID
	defer s.gs.ReleasePushMirror(runID)
	out, _ := q.exec(ctx, `cd /home/ide/workspace && git config --get quorum.base || true`)
	q.g.baseBranch = strings.TrimSpace(lastLineStr(out))
	rows, err := s.store.q.ListQuorumEvents(ctx, pgUUID(runID))
	if err != nil || len(rows) == 0 {
		return QuorumPublishResult{}, qErr(404, "this run has no Quorum ledger")
	}
	events := make([]quorum.Event, 0, len(rows))
	for _, r := range rows {
		var e quorum.Event
		if err := json.Unmarshal([]byte(r.Body), &e); err != nil {
			return QuorumPublishResult{}, err
		}
		events = append(events, e)
	}
	k, err := quorum.Replay(events, quorum.Options{Sink: q.persist})
	if err != nil {
		return QuorumPublishResult{}, err
	}
	q.k = k
	v := k.View()
	if v.Run.Status != quorum.RunStopped {
		return QuorumPublishResult{}, qErr(409, "the run is %s; only a stopped run publishes", v.Run.Status)
	}
	pushed, merged := q.publishFinal(ctx, v)
	if merged != "" {
		q.emit(ctx, Event{Type: "q.publish", Title: merged})
		if r, err := s.store.GetRunByID(ctx, runID); err == nil {
			r.Result.Narrative = strings.TrimSpace(r.Result.Narrative + "\n\n" + merged)
			_ = s.store.UpdateRunSwarm(ctx, r)
		}
	}
	return QuorumPublishResult{Pushed: pushed, Merged: merged}, nil
}

func (q *qRun) finalize(ctx context.Context) {
	if !q.finished.CompareAndSwap(false, true) {
		return
	}
	v := q.k.View()
	// Persist the model cards to the Commons.
	for _, r := range q.k.Reputations() {
		card, _ := json.Marshal(r)
		_ = q.s.store.q.UpsertQuorumReputation(ctx, db.UpsertQuorumReputationParams{WorkspaceID: pgUUID(q.wsID), LineageID: r.Lineage, ClaimType: r.ClaimType, Card: card})
	}
	if q.abort.Load() || v.Run.Status == quorum.RunAborted {
		run, err := q.s.store.GetRunByID(ctx, q.runID)
		if err == nil {
			run.Status, run.Error = RunAborted, firstNonEmpty(q.stopMsg, v.Run.StoppedReason, "stopped by operator")
			run.VerdictSentence, run.EndedAt = run.Error, time.Now()
			run.Result = Assessment{Achieved: false, Narrative: run.Error, SpendUSD: round2(v.Metrics.SpendUSD)}
			_ = q.s.store.UpdateRunSwarm(ctx, run)
		}
		q.emit(ctx, Event{Type: EvRunFailed, Level: "warn", Title: firstNonEmpty(q.stopMsg, "stopped by operator")})
		return
	}
	ready := v.Run.Status == quorum.RunStopped && v.Run.StoppedReason == "ready"
	pushed, merged := q.publishFinal(ctx, v)
	v = q.k.View()
	rep := v.Report
	a := Assessment{SpendUSD: round2(v.Metrics.SpendUSD), Achieved: ready}
	var sb strings.Builder
	if rep != nil {
		fmt.Fprintf(&sb, "Quorum stopped (%s). Coverage %.0f%%, %d events on a verified ledger, adherence %.0f%%.", rep.Reason, v.Metrics.Coverage*100, v.Events, rep.Adherence*100)
		if rep.Statement != "" {
			sb.WriteString("\n\n" + rep.Statement)
		}
		if rep.Rendering != "" {
			sb.WriteString("\n\n" + truncate(rep.Rendering, 6000))
		}
		if len(rep.Gaps) > 0 {
			sb.WriteString("\n\nGaps:\n- " + strings.Join(rep.Gaps, "\n- "))
		}
	}
	if merged != "" {
		sb.WriteString("\n\n" + merged)
	} else if pushed {
		sb.WriteString("\n\nThe integration branch " + q.integrationBranch() + " is pushed.")
	}
	a.Narrative = sb.String()
	run, err := q.s.store.GetRunByID(ctx, q.runID)
	if err == nil {
		q.run = run
	}
	if ready {
		q.run.Status, q.run.Progress, q.run.Error = RunSolved, 1, ""
		q.run.VerdictSentence = fmt.Sprintf("Ready: every acceptance criterion has an admitted claim (%.0f%% coverage) on %s.", v.Metrics.Coverage*100, q.integrationBranch())
	} else {
		q.run.Status, q.run.Error = RunStuck, firstNonEmpty(v.Run.StoppedReason, "stopped")
		q.run.VerdictSentence = "Stopped: " + firstNonEmpty(v.Run.StoppedReason, "the run ended")
	}
	q.run.Result, q.run.EndedAt = a, time.Now()
	_ = q.s.store.UpdateRunSwarm(ctx, q.run)
	_, _ = q.s.store.CreateSwarmArtifact(ctx, q.wsID, q.runID, "", "", SwarmArtifact{Kind: "assessment", Title: "Quorum report", Summary: truncate(a.Narrative, 200), Status: "validated", Body: mustJSON(a)})
	if ready {
		q.emit(ctx, Event{Type: EvRunDone, Title: "Quorum ready: " + firstNonEmpty(merged, "integration branch pushed"), Body: map[string]any{"spend_usd": a.SpendUSD}})
	} else {
		q.emit(ctx, Event{Type: EvRunFailed, Level: "warn", Title: "Quorum stopped: " + firstNonEmpty(v.Run.StoppedReason, "ended"), Body: map[string]any{"spend_usd": a.SpendUSD}})
	}
}

// qNumbers mirrors the kernel's quantitative-token extraction.
func qNumbers(s string) []string {
	var out []string
	for _, f := range strings.FieldsFunc(s, func(r rune) bool { return !(r >= '0' && r <= '9' || r == '.' || r == ',' || r == '%') }) {
		f = strings.Trim(f, ".,")
		if len(strings.TrimSuffix(f, "%")) > 1 {
			if _, err := strconv.ParseFloat(strings.ReplaceAll(strings.TrimSuffix(f, "%"), ",", ""), 64); err == nil {
				out = append(out, f)
			}
		}
	}
	return out
}
