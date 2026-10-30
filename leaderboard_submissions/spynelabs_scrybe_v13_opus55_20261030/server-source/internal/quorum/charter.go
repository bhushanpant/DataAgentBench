package quorum

import (
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"fmt"
	"strings"

	"gopkg.in/yaml.v3"
)

// Charter is the declarative policy of a run (spec §14). It is immutable once
// the run starts; its content address is recorded on run.created.
type Charter struct {
	Version      int                 `json:"charter_version" yaml:"charter_version"`
	Mode         string              `json:"mode" yaml:"mode"`                     // market | lite
	Size         string              `json:"size,omitempty" yaml:"size,omitempty"` // solo | team | swarm (applySize); empty keeps the defaults
	Run          CharterRun          `json:"run" yaml:"run"`
	Lineages     []CharterLineage    `json:"lineages" yaml:"lineages"`
	Market       CharterMarket       `json:"market" yaml:"market"`
	Admission    CharterAdmission    `json:"admission" yaml:"admission"`
	Verification CharterVerification `json:"verification" yaml:"verification"`
	Governance   CharterGovernance   `json:"governance" yaml:"governance"`
	Stop         CharterStop         `json:"stop" yaml:"stop"`
	SideEffects  []CharterSideEffect `json:"side_effects" yaml:"side_effects"`
}

type CharterRun struct {
	MaxAgents       int                `json:"max_agents" yaml:"max_agents"`
	Budget          CharterBudget      `json:"budget" yaml:"budget"`
	Pools           map[string]float64 `json:"pools" yaml:"pools"`
	VerifyFloor     float64            `json:"verify_floor" yaml:"verify_floor"`
	AssignmentSec   int                `json:"assignment_sec" yaml:"assignment_sec"`
	MaxOpenBounties int                `json:"max_open_bounties" yaml:"max_open_bounties"`
}

type CharterBudget struct {
	USD     float64 `json:"usd" yaml:"usd"`
	Tokens  int64   `json:"tokens" yaml:"tokens"`
	WallSec int     `json:"wall_clock_s" yaml:"wall_clock_s"`
}

type CharterLineage struct {
	Family  string `json:"family" yaml:"family"`
	Model   string `json:"model" yaml:"model"`
	Harness string `json:"harness" yaml:"harness"`
	Slots   int    `json:"slots" yaml:"slots"`
	Persona string `json:"persona,omitempty" yaml:"persona,omitempty"`
	Label   string `json:"label,omitempty" yaml:"label,omitempty"`
	AgentID string `json:"agent_id,omitempty" yaml:"agent_id,omitempty"`
}

type CharterMarket struct {
	BidWindowSec       float64 `json:"bid_window_s" yaml:"bid_window_s"`
	KMinBids           int     `json:"k_min_bids" yaml:"k_min_bids"`
	LambdaDiversity    float64 `json:"lambda_diversity" yaml:"lambda_diversity"`
	MuFamily           float64 `json:"mu_family" yaml:"mu_family"`
	KappaCriticalPath  float64 `json:"kappa_critical_path" yaml:"kappa_critical_path"`
	RhoMin             float64 `json:"rho_min" yaml:"rho_min"`
	DecompInflationCap float64 `json:"decomp_inflation_cap" yaml:"decomp_inflation_cap"`
	MaxBidRounds       int     `json:"max_bid_rounds" yaml:"max_bid_rounds"`
	// FastDecompose plans the root decomposition with one structured model
	// call over the repository listing and attached data instead of an agent
	// session; the runner falls back to a session when the call fails.
	FastDecompose bool `json:"fast_decompose" yaml:"fast_decompose"`
	// Router names an external judge that scores every bounty per lineage
	// ("jev"); empty keeps bid rounds. With a router the kernel assigns a
	// bounty to the lineage with the lowest expected cost per success,
	// (price + route_overhead[risk]) / p, turns a build bounty the judge calls
	// too broad into a decompose bounty (split_p, at most max_split_depth
	// levels below the root plan), and falls back to the market when no
	// advice arrives within route_wait_s.
	Router        string             `json:"router,omitempty" yaml:"router,omitempty"`
	RouteOverhead map[string]float64 `json:"route_overhead,omitempty" yaml:"route_overhead,omitempty"`
	SplitP        float64            `json:"split_p,omitempty" yaml:"split_p,omitempty"`
	MaxSplitDepth int                `json:"max_split_depth,omitempty" yaml:"max_split_depth,omitempty"`
	RouteWaitSec  float64            `json:"route_wait_s,omitempty" yaml:"route_wait_s,omitempty"`
	// SplitEstSec turns a posted build bounty estimated at this many seconds
	// or more into a decompose bounty (at most max_split_depth levels), so a
	// swarm keeps splitting work until each part fits one agent. Without a
	// router this is the only split policy; 0 means 1200, negative disables.
	SplitEstSec float64 `json:"split_est_s,omitempty" yaml:"split_est_s,omitempty"`
}

type CharterAdmission struct {
	MaxBaseLagCommits int `json:"max_base_lag_commits" yaml:"max_base_lag_commits"`
	LeaseTTLSec       int `json:"lease_ttl_s" yaml:"lease_ttl_s"`
}

type CharterVerification struct {
	AllocTickSec           float64        `json:"alloc_tick_s" yaml:"alloc_tick_s"`
	MinIndependentVerdicts map[string]int `json:"min_independent_verdicts" yaml:"min_independent_verdicts"`
	MinFamiliesHighRisk    int            `json:"min_families_high_risk" yaml:"min_families_high_risk"`
	AdmitThreshold         float64        `json:"admit_threshold" yaml:"admit_threshold"`
	RetractThreshold       float64        `json:"retract_threshold" yaml:"retract_threshold"`
	MinGain                float64        `json:"min_gain" yaml:"min_gain"`
	// ChecksReplaceVerifier lets a deterministic AC check stand in for one
	// verifier: a code claim whose every cited AC has a check that passed at a
	// strong gate needs one verdict fewer. Use it when the AC checks test the
	// substance (data reconciliation), not when they only test structure.
	ChecksReplaceVerifier bool         `json:"checks_replace_verifier,omitempty" yaml:"checks_replace_verifier,omitempty"`
	Gates                 CharterGates `json:"gates" yaml:"gates"`
}

type CharterGates struct {
	CodeChange CharterCodeGate `json:"code_change" yaml:"code_change"`
}

type CharterCodeGate struct {
	Tests                        string `json:"tests" yaml:"tests"`
	Lint                         string `json:"lint" yaml:"lint"`
	ForbidTestEditsWithoutIntent bool   `json:"forbid_test_edits_without_intent" yaml:"forbid_test_edits_without_intent"`
	TimeoutSec                   int    `json:"timeout_s" yaml:"timeout_s"`
}

type CharterGovernance struct {
	ContestStake         float64              `json:"contest_stake" yaml:"contest_stake"`
	ContestThreshold     float64              `json:"contest_threshold" yaml:"contest_threshold"`
	ClusterFreeze        CharterClusterFreeze `json:"cluster_freeze" yaml:"cluster_freeze"`
	PageAckTimeoutSec    int                  `json:"page_ack_timeout_s" yaml:"page_ack_timeout_s"`
	ContestPageThreshold int                  `json:"contest_page_threshold" yaml:"contest_page_threshold"`
	ChannelRatePerMin    int                  `json:"channel_rate_per_min" yaml:"channel_rate_per_min"`
}

type CharterClusterFreeze struct {
	K              int     `json:"k" yaml:"k"`
	WindowSec      float64 `json:"window_s" yaml:"window_s"`
	MinhashJaccard float64 `json:"minhash_jaccard" yaml:"minhash_jaccard"`
}

type CharterStop struct {
	TickSec          float64 `json:"tick_s" yaml:"tick_s"`
	CoverageRequired float64 `json:"coverage_required" yaml:"coverage_required"`
	MVoIBurnTheta    float64 `json:"mvoi_burn_theta" yaml:"mvoi_burn_theta"`
	Nominations      bool    `json:"nominations" yaml:"nominations"`
	// KernelSynthesis lets the kernel write the final synthesis itself when
	// every required AC is covered by admitted code claims: the answer is then
	// a projection of verified work, so no integrator session or synthesis
	// verification is needed. Runs that rest on facts or analyses keep the
	// integrator.
	KernelSynthesis bool `json:"kernel_synthesis" yaml:"kernel_synthesis"`
}

type CharterSideEffect struct {
	Connector string `json:"connector" yaml:"connector"`
	Op        string `json:"op" yaml:"op"`
	Policy    string `json:"policy" yaml:"policy"` // deny | auto | human
}

// DefaultCharter is the spec §14 example scaled to one sandbox.
func DefaultCharter() Charter {
	return Charter{
		Version: 1,
		Mode:    "market",
		Run: CharterRun{
			MaxAgents:       12,
			Budget:          CharterBudget{USD: 20, WallSec: 3600},
			Pools:           map[string]float64{"build": 0.55, "verify": 0.25, "governance": 0.10, "integrate": 0.10},
			VerifyFloor:     0.15,
			AssignmentSec:   1500,
			MaxOpenBounties: 64,
		},
		// One tier by default: with mixed tiers and no router, cheap bids win
		// planning and integration and runs stall (measured 2026-10-20). Mixed
		// tiers belong with market.router.
		Lineages: []CharterLineage{
			{Family: "anthropic", Model: "claude-sonnet-5-5", Harness: "opencode", Slots: 12, Label: "Sonnet"},
		},
		Market: CharterMarket{
			BidWindowSec: 20, KMinBids: 3, LambdaDiversity: 0.5, MuFamily: 0.3,
			KappaCriticalPath: 1.0, RhoMin: 0.5, DecompInflationCap: 1.2, MaxBidRounds: 4, FastDecompose: true,
		},
		Admission: CharterAdmission{MaxBaseLagCommits: 3, LeaseTTLSec: 900},
		Verification: CharterVerification{
			AllocTickSec:           10,
			MinIndependentVerdicts: map[string]int{RiskLow: 0, RiskMedium: 1, RiskHigh: 2},
			MinFamiliesHighRisk:    2,
			AdmitThreshold:         0.8,
			RetractThreshold:       0.25,
			MinGain:                0.02,
			Gates:                  CharterGates{CodeChange: CharterCodeGate{ForbidTestEditsWithoutIntent: true, TimeoutSec: 600}},
		},
		Governance: CharterGovernance{
			ContestStake: 0.02, ContestThreshold: 0.5,
			ClusterFreeze:     CharterClusterFreeze{K: 4, WindowSec: 300, MinhashJaccard: 0.6},
			PageAckTimeoutSec: 900, ContestPageThreshold: 6, ChannelRatePerMin: 6,
		},
		Stop: CharterStop{TickSec: 60, CoverageRequired: 1.0, MVoIBurnTheta: 0.15, KernelSynthesis: true},
		SideEffects: []CharterSideEffect{
			{Connector: "github", Op: "create_pull_request", Policy: "human"},
			{Connector: "http", Op: "*", Policy: "deny"},
		},
	}
}

// ParseCharter overlays a JSON or YAML document on the defaults. Unknown keys
// are rejected so a typo cannot silently weaken a gate. The document's size
// preset applies to the defaults first, so values the document sets win.
func ParseCharter(doc string) (Charter, error) {
	c := DefaultCharter()
	doc = strings.TrimSpace(doc)
	if doc == "" {
		return c, nil
	}
	var sized struct {
		Size string `json:"size" yaml:"size"`
	}
	if strings.HasPrefix(doc, "{") {
		_ = json.Unmarshal([]byte(doc), &sized)
	} else {
		_ = yaml.Unmarshal([]byte(doc), &sized)
	}
	c.Size = sized.Size
	c.applySize()
	if strings.HasPrefix(doc, "{") {
		dec := json.NewDecoder(strings.NewReader(doc))
		dec.DisallowUnknownFields()
		if err := dec.Decode(&c); err != nil {
			return c, fmt.Errorf("charter: %w", err)
		}
	} else {
		dec := yaml.NewDecoder(strings.NewReader(doc))
		dec.KnownFields(true)
		if err := dec.Decode(&c); err != nil {
			return c, fmt.Errorf("charter: %w", err)
		}
	}
	return c, c.Validate()
}

// Charter sizes (Charter.Size).
const (
	SizeSolo  = "solo"
	SizeTeam  = "team"
	SizeSwarm = "swarm"
)

// applySize sets the preset of the charter's size: solo is one agent with no
// splitting and no fast root plan, team is three agents with no splitting, and
// swarm (or no size) keeps the market defaults.
func (c *Charter) applySize() {
	switch c.Size {
	case SizeSolo:
		c.Mode, c.Run.MaxAgents, c.Market.SplitEstSec, c.Market.FastDecompose = "lite", 1, -1, false
	case SizeTeam:
		c.Mode, c.Run.MaxAgents, c.Market.SplitEstSec = "lite", 3, -1
	}
}

// Validate rejects charters the kernel cannot run safely.
func (c *Charter) Validate() error {
	if c.Market.Router != "" && c.Market.Router != "jev" {
		return fmt.Errorf("charter: market.router must be empty or jev")
	}
	if c.Mode != "market" && c.Mode != "lite" {
		return fmt.Errorf("charter: mode must be market or lite")
	}
	switch c.Size {
	case "", SizeSolo, SizeTeam, SizeSwarm:
	default:
		return fmt.Errorf("charter: size must be solo, team or swarm")
	}
	if len(c.Lineages) == 0 {
		return fmt.Errorf("charter: at least one lineage is required")
	}
	for i, l := range c.Lineages {
		if strings.TrimSpace(l.Model) == "" || l.Slots <= 0 {
			return fmt.Errorf("charter: lineage %d needs a model and at least one slot", i+1)
		}
	}
	if c.Run.Budget.USD <= 0 || c.Run.Budget.WallSec <= 0 {
		return fmt.Errorf("charter: budget.usd and budget.wall_clock_s must be positive")
	}
	sum := 0.0
	for _, v := range c.Run.Pools {
		if v < 0 {
			return fmt.Errorf("charter: pool shares must not be negative")
		}
		sum += v
	}
	if sum < 0.99 || sum > 1.01 {
		return fmt.Errorf("charter: pool shares must sum to 1 (got %.2f)", sum)
	}
	if c.Run.Pools["verify"] < c.Run.VerifyFloor {
		return fmt.Errorf("charter: the verify pool is below verify_floor")
	}
	if c.Run.MaxAgents <= 0 {
		c.Run.MaxAgents = 1
	}
	for _, se := range c.SideEffects {
		switch se.Policy {
		case "deny", "auto", "human":
		default:
			return fmt.Errorf("charter: side effect %s/%s has unknown policy %q", se.Connector, se.Op, se.Policy)
		}
	}
	return nil
}

// CID is the charter's content address.
func (c Charter) CID() string {
	b, _ := json.Marshal(c)
	sum := sha256.Sum256(b)
	return hex.EncodeToString(sum[:])
}

// YAML renders the charter for the operator console.
func (c Charter) YAML() string {
	b, err := yaml.Marshal(c)
	if err != nil {
		return ""
	}
	return string(b)
}

// TotalSlots is the worker pool size.
func (c Charter) TotalSlots() int {
	n := 0
	for _, l := range c.Lineages {
		n += l.Slots
	}
	return n
}

// sideEffectPolicy is the most specific matching policy; everything defaults
// to deny.
func (c Charter) sideEffectPolicy(connector, op string) string {
	policy := "deny"
	for _, se := range c.SideEffects {
		if se.Connector == connector && se.Op == op {
			return se.Policy
		}
		if se.Connector == connector && se.Op == "*" {
			policy = se.Policy
		}
	}
	return policy
}
