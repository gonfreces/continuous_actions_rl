import numpy as np
import random
import torch
import torch.optim as optim
import torch.nn.functional as F
import torch.nn.utils as U
from collections import namedtuple, deque
from PPO_2_models import PPO_ActorCritic

##### CONFIG PARMAS #####
BUFFER_SIZE = int(5e4)        # buffer size of memory storage
BATCH_SIZE = 2048             # batch size of sampling
MIN_BUFFER_SIZE = BATCH_SIZE  # min buffer size before learning starts
GAMMA = 0.99                  # discount factor
T_MAX = 2048                  # max number of time step
LR = 1e-4                     # learning rate #5e-4
GRAD_CLIP_MAX = 1.0           # max gradient allowed
CRITIC_L_WEIGHT = 0.5         # mean square error term weight
ENT_WEIGHT = 0.02             # weight of entropy added
ENT_DECAY = 0.999             # decay of entropy per 'step'
ENT_MIN = 1e-4                # min weight of entropy
STD_SCALE_INIT = 1.0          # initial value of std scale for action resampling
#STD_SCALE_DECAY = 0.9994      # scale decay of std
#STD_SCALE_MIN = 0.1           # min value of STD scale
LEARN_EVERY = 1               # no of step until next update
LEARNING_LOOP = 5             # no of learning on grad per update
P_RATIO_EPS = 0.2             # eps for ratio clip 1+eps, 1-eps
USE_GAE = True                # use GAE flag
GAE_TAU = 0.95                # value control how much agent rely on current estimate
USE_HUBER = False             # use huber loss as loss function?

device = torch.device("cuda:0" if torch.cuda.is_available() else "cpu")

class PPO_Agent():
    """Interacts with and learns from the environment."""

    def __init__(self, env, state_size, action_size, num_agents=12, seed=0):
        """Initialize an Agent object.
        Params
        ======
            env (env object): object of the env
            state_size (int): dimension of each state
            action_size (int): dimension of each action
            num_agents (int): number of agents run in parallel
            seed (int): random seed
        """
        self.env = env
        self.brain_name = self.env.brain_names[0]
        self.state_size = state_size
        self.action_size = action_size
        self.num_agents = num_agents
        self.seed = random.seed(seed)
        self.gamma = GAMMA # discount rate
        self.ent_weight = ENT_WEIGHT
        self.t_max = T_MAX # max number of steps for episodic exploration

        # Replay memory
        self.memory = ReplayBuffer(BUFFER_SIZE, BATCH_SIZE, action_size,
                                   num_agents,seed)

        # Init Network Models and Optimizers
        self.model_local = PPO_ActorCritic(state_size, action_size, device, seed).to(device)
        self.optim = optim.Adam(self.model_local.parameters(), lr=LR, weight_decay=0.0)
        #self.optim_actor = optim.Adam(self.model_local.actor.parameters(), lr=ACTOR_LR)
        #self.optim_critic = optim.Adam(self.model_local.critic.parameters(), lr=CRITIC_LR, weight_decay=0.)

        # Initialize time step (for updating every UPDATE_EVERY steps and others)
        self.t_step = 0

        # std for noise
        self.std_scale = STD_SCALE_INIT

        # for tracking
        self.episodic_rewards = deque(maxlen=100000) # hist of rewards total of DONE episodes
        self.running_rewards = np.zeros(self.num_agents)
        self.running_trajectory = [] # record info about the current trajectory

        # global record for normalizers
        self.r_history = deque(maxlen=100000)

        # for tracking
        self.critic_loss_hist = deque(maxlen=100)
        self.actor_gain_hist = deque(maxlen=100)
        self.entropy_hist = deque(maxlen=100)

        # training or just accumulating experience?
        self.is_training = False

        print("current device: ", device)


    def _toTorch(self, s, dtype=torch.float32):
        return torch.tensor(s, dtype=dtype, device=device)

    def r_normalizer(self, data):
        self.r_history.extend(np.array(data).flatten())
        return [(d-np.mean(self.r_history))/np.std(self.r_history) for d in data]


    def _collect_trajectory_data(self, train_mode=True):
        """
        Collect trajectory data and store them
        output: tuple of list (len: len(states)) of:
                states, log_probs, actions, rewards, As, TDs
        """

        # reset env and running reward
        env_info = self.env.reset(train_mode=train_mode)[self.brain_name] # reset env
        self.running_rewards = np.zeros(self.num_agents)

        s, p, a, r, ns, d, A, V, td = ([] for l in range(9)) #initialization
        # s, ns: TENSOR: num_agents x state_size (129)
        # log_prob: #TENSOR: num_agents x 1, require grad
        # action: np.array: num_agents x action_size
        # reward, done: np.array: num_agents x 1
        # V or Q: TENSOR: num_agents x 1, require grad

        # initial state
        state = self._toTorch(env_info.vector_observations) # tensor: num_agents x state_size

        # Collect the STEP trajectory data (s,a,r,ns,d)
        ep_len = 0
        while ep_len < T_MAX:
            # state -> prob / actions
            state_predict = self.model_local(state, std_scale=self.std_scale)

            action = state_predict['a'] #array, num_agents x action_size no grad
            action = np.clip(action, -1, 1)

            env_info = self.env.step(action)[self.brain_name]

            next_state = self._toTorch(env_info.vector_observations) # tensor: num_agents x state_size
            reward = np.array(env_info.rewards) # array: (num_agents,)
            done = np.array(env_info.local_done) #array: (num_agents,)

            # recognize the current reward first
            self.running_rewards += reward

            s.append(state) #TENSOR: num_agents x state_size (129)
            p.append(state_predict['log_prob']) #tensor: (num_agents x 1), require grad
            a.append(action) #np.array: num_agents x action_size
            r.append(np.array(reward).reshape(-1,1)) #array: num_agents x 1
            ns.append(next_state) #TENSOR: num_agents x state_size (129)
            d.append(np.array(done).reshape(-1,1)) #array: num_agents x 1
            V.append(state_predict['v']) #Q value TENSOR: num_agents x 1, require grad

            if np.any(done): # if all are done simultaneously
                self.episodic_rewards.append(np.mean(self.running_rewards))
                break # this actually never happens

            state = next_state
            ep_len += 1


        # normalize reward
        r = self.r_normalizer(r)

        # Compute the Advantage/Return value
        # note that last state has no entry in record in V
        last_state_predict = self.model_local(next_state)

        # use td target as return, td error as advantage
        V.append(last_state_predict['v']) #range(ep_len) > len(V) by 1 as last state is added

        advantage = np.zeros([self.num_agents, 1])
        td_target = last_state_predict['v'].detach().numpy()
        for i in reversed(range(ep_len)):
            # effect of this loop is similar future credit assignments
            td_target = r[i] + GAMMA * (1-d[i]) * td_target
            td_current = V[i].detach().numpy()
            if not USE_GAE:
                advantage = td_target - td_current
            else:
                td_error = r[i] + GAMMA * (1-d[i]) * V[i+1].detach().numpy() - td_current
                advantage = advantage*GAE_TAU*GAMMA*(1-d[i]) + td_error
            A.append(advantage) #array:, num_agents x 1, no grad
            td.append(td_target) #array:, num_agents x 1, no grad

        # reverse back the list
        A = [a for a in reversed(A)]
        td = [td_i for td_i in reversed(td)]

        # normalize Advantage and tds
        A = [(a-np.mean(A))/np.std(A) for a in A]
        td = [(t-np.mean(td))/np.std(td) for t in td]

        # store data in memory
        data = (s, p, a, r, A, td)
        self.memory.add(data) #tuple of list by types of data NOT EXPERIENCE

        return


    def step(self, train_mode=True):
        """ a step of collecting, sampling data and learn from it
            eps: for exploration if external noise is added
            train_mode: for the env
        """

        self._collect_trajectory_data(train_mode=train_mode)

        if train_mode and len(self.memory) >= MIN_BUFFER_SIZE:
            if self.is_training == False:
                print("Prefetch completed. Training starts! \r")
                print("Number of Agents: ", self.num_agents)
                print("Device: ", device)
                self.is_training = True

            if self.t_step % LEARN_EVERY == 0:
                for _ in range(LEARNING_LOOP):
                    sampled_data = self.memory.sample() #sample from memory
                    self.learn(sampled_data) #learn from it and update grad

        self.t_step += 1

    def learn(self, m_batch):
        """Update the parameters of the policy based on the data in the sampled
           trajectory.
        Params
        ======
        inputs:
            m_batch: (tuple) of:
                batch of states: (tensor) batch_size or num_agents x state_size
                batch of old_probs: (tensor) batch_size or num_agents x 1
                batch of actions: (tensor) batch_size or num_agents x state_size
                batch of rewards: (tensor) batch_size or num_agents x 1
                batch of Advantages: (tensor) batch_size or num_agents x 1
                batch of Returns/TDs: (tensor) batch_size or num_agents x 1
        """
        s, p, a, r, A, td = m_batch

        ############################# ACTOR LOSS ##############################
        old_prob = p.detach() # num_agents, no grad
        s_predictions = self.model_local(s, a) #use old s, a to get new prob
        new_prob = s_predictions['log_prob'] # num_agents x 1
        assert(new_prob.requires_grad == True)
        assert(A.requires_grad == False)

        ratio = (new_prob - old_prob).exp() # # num_agent or m x 1

        G = ratio * A

        G_clipped = torch.clamp(ratio, min=1.-P_RATIO_EPS,
                                       max=1.+P_RATIO_EPS) * A

        G_loss = torch.min(G, G_clipped).mean()

        G_entropy = self.ent_weight * s_predictions['ent'].mean()

        actor_loss = -(G_loss + G_entropy)

        ############################ CRITIC LOSS ##############################
        td_current = s_predictions['v'] # # num_agent or m x 1, requires grad
        assert(td_current.requires_grad == True)

        td_target = td

        if USE_HUBER:
            huber_loss = torch.nn.SmoothL1Loss()
            critic_loss = huber_loss(td_current, td_target)
        else:
            critic_loss = 0.5 * (td_target - td_current).pow(2).mean()
        #critic_loss = CRITIC_L_WEIGHT * critic_loss
        # TOTAL LOSS
        total_loss = actor_loss + CRITIC_L_WEIGHT*critic_loss

        self.optim.zero_grad()
        total_loss.backward() #retain_graph=True
        U.clip_grad_norm_(self.model_local.parameters(), GRAD_CLIP_MAX)
        self.optim.step()


        self.actor_gain_hist.append(-actor_loss.data.detach().numpy())
        self.critic_loss_hist.append(critic_loss.data.detach().numpy())
        self.entropy_hist.append(G_entropy.mean().detach().numpy())

        # entropy weight decay
        self.ent_weight = max(self.ent_weight * ENT_DECAY, ENT_MIN)
        # std decay
        #self.std_scale = max(self.std_scale * STD_SCALE_DECAY, STD_SCALE_MIN)


class ReplayBuffer:
    """Fixed-size buffer to store experience tuples."""

    def __init__(self, buffer_size, batch_size, action_size, num_agents, seed=0):
        """Data Structure to store experience object.
        Params
        ======
            buffer_size (int): maximum size of buffer
            batch_size (int): size of each training batch
            seed (int): random seed
        """
        self.buffer_size = buffer_size
        self.memory = deque(maxlen=buffer_size)
        self.batch_size = batch_size
        self.action_size = action_size
        self.num_agents = num_agents

        # data structure for storing individual experience
        self.data = namedtuple("data", field_names=["states", "old_probs",
                                                    "actions", "rewards",
                                                    "As", "returns"])
        torch.manual_seed(seed)


    def add(self, single_traj_data):
        """ Add a new experience to memory.
            data: (tuple) states, log_probs, rewards, As, Vs
        """
        (s_, p_, a_, r_, A_, td_) = single_traj_data #equal lengths

        for s, p, a, r, A, td in zip(s_, p_, a_, r_, A_, td_): #by time step
            i = 0
            while i < self.num_agents: #by agent
                e = self.data(s[i,:], p[i,:], a[i,:], r[i], A[i], td[i])
                self.memory.append(e)
                i += 1


    def sample(self):
        """Sample a batch of experiences from memory."""
        # get sample of index from the p distribution
        sample_ind = np.random.choice(len(self.memory), self.batch_size)

        # get the selected experiences: avoid using mid list indexing
        s_s, s_p, s_a, s_r, s_A, s_td = ([] for l in range(6))

        i = 0
        while i < len(sample_ind): #while loop is faster
            self.memory.rotate(-sample_ind[i])

            e = self.memory[0]
            s_s.append(e.states)
            s_p.append(e.old_probs)
            s_a.append(e.actions)
            s_r.append(e.rewards)
            s_A.append(e.As)
            s_td.append(e.returns)
            self.memory.rotate(sample_ind[i])
            i += 1

        # change the format to tensor and make sure dims are correct for calculation
        s_s = torch.stack(s_s).float().to(device)
        s_p = torch.stack(s_p).to(device)
        s_a = torch.from_numpy(np.stack(s_a)).float().to(device)
        s_r = torch.from_numpy(np.vstack(s_r)).float().to(device)
        s_A = torch.from_numpy(np.stack(s_A)).float().to(device)
        s_td = torch.from_numpy(np.stack(s_td)).float().to(device)

        return (s_s, s_p, s_a, s_r, s_A, s_td)

    def __len__(self):
        """Return the current size of internal memory."""
        return len(self.memory)
