from data.utils import load_data_torch
from src.utils import *
from src.layers import *
import pickle
import sys
import time



with open('./TIP/data/decagon_et.pkl', 'rb') as f:   # the whole dataset
    et_list = pickle.load(f)

out_dir = './TIP/qu_out/ggm/'

EPOCH_NUM = 100

#########################################################################
# et_list = et_list[-200:]
#########################################################################

feed_dict = load_data_torch("./TIP/data/", et_list, mono=True)

[n_drug, n_feat_d] = feed_dict['d_feat'].shape
n_et_dd = len(et_list)

data = Data.from_dict(feed_dict)


data.train_idx, data.train_et, data.train_range,data.test_idx, data.test_et, data.test_range = process_edges(data.dd_edge_index)

# TODO: add drug feature
data.d_feat = sparse_id(n_drug)
n_feat_d = n_drug
data.x_norm = torch.ones(n_drug)
# data.x_norm = torch.sqrt(data.d_feat.sum(dim=1))

n_base = 16

n_embed = 16
n_hid1 = 64
n_hid2 = 32


class Encoder(torch.nn.Module):

    def __init__(self, in_dim, num_et, num_base):
        super(Encoder, self).__init__()
        self.num_et = num_et

        self.embed = Param(torch.Tensor(in_dim, n_embed))

        self.reset_paramters()

    def forward(self, x, edge_index, edge_type, range_list, x_norm):
        x = torch.matmul(x, self.embed)
        x = x / x_norm.view(-1, 1)

        x = F.relu(x, inplace=True)
        return x

    def reset_paramters(self):
        self.embed.data.normal_()


class MultiInnerProductDecoder(torch.nn.Module):
    def __init__(self, in_dim, num_et):
        super(MultiInnerProductDecoder, self).__init__()
        self.num_et = num_et
        self.in_dim = in_dim
        self.weight = Param(torch.Tensor(num_et, in_dim))

        self.reset_parameters()

    def forward(self, z, edge_index, edge_type, sigmoid=True):
        value = (z[edge_index[0]] * z[edge_index[1]] * self.weight[edge_type]).sum(dim=1)
        return torch.sigmoid(value) if sigmoid else value

    def reset_parameters(self):
        self.weight.data.normal_(std=1/np.sqrt(self.in_dim))


encoder = Encoder(n_feat_d, n_et_dd, n_base)
decoder = MultiInnerProductDecoder(n_embed, n_et_dd)
model = MyGAE(encoder, decoder)

device_name = 'cuda' if torch.cuda.is_available() else 'cpu'
print(device_name)
device = torch.device(device_name)

model = model.to(device)
optimizer = torch.optim.Adam(model.parameters(), lr=0.01)
data = data.to(device)

train_record = {}
test_record = {}
train_out = {}
test_out = {}


@profile
def train():
    model.train()

    optimizer.zero_grad()
    z = model.encoder(data.d_feat, data.train_idx, data.train_et, data.train_range, data.x_norm)

    pos_index = data.train_idx
    neg_index = typed_negative_sampling(data.train_idx, n_drug, data.train_range).to(device)

    pos_score = model.decoder(z, pos_index, data.train_et)
    neg_score = model.decoder(z, neg_index, data.train_et)

    # pos_loss = F.binary_cross_entropy(pos_score, torch.ones(pos_score.shape[0]).cuda())
    # neg_loss = F.binary_cross_entropy(neg_score, torch.ones(neg_score.shape[0]).cuda())
    pos_loss = -torch.log(pos_score + EPS).mean()
    neg_loss = -torch.log(1 - neg_score + EPS).mean()
    loss = pos_loss + neg_loss
    # loss = pos_loss

    loss.backward()
    optimizer.step()

    record = np.zeros((3, n_et_dd))  # auprc, auroc, ap
    for i in range(data.train_range.shape[0]):
        [start, end] = data.train_range[i]
        p_s = pos_score[start: end]
        n_s = neg_score[start: end]

        pos_target = torch.ones(p_s.shape[0])
        neg_target = torch.zeros(n_s.shape[0])

        score = torch.cat([p_s, n_s])
        target = torch.cat([pos_target, neg_target])

        record[0, i], record[1, i], record[2, i] = auprc_auroc_ap(target, score)

    train_record[epoch] = record
    [auprc, auroc, ap] = record.sum(axis=1) / n_et_dd
    train_out[epoch] = [auprc, auroc, ap]

    print('{:3d}   loss:{:0.4f}   auprc:{:0.4f}   auroc:{:0.4f}   ap@50:{:0.4f}'
          .format(epoch, loss.tolist(), auprc, auroc, ap))

    return z, loss


test_neg_index = typed_negative_sampling(data.test_idx, n_drug, data.test_range).to(device)


def test(z):
    model.eval()

    record = np.zeros((3, n_et_dd))     # auprc, auroc, ap

    pos_score = model.decoder(z, data.test_idx, data.test_et)
    neg_score = model.decoder(z, test_neg_index, data.test_et)

    for i in range(data.test_range.shape[0]):
        [start, end] = data.test_range[i]
        p_s = pos_score[start: end]
        n_s = neg_score[start: end]

        pos_target = torch.ones(p_s.shape[0])
        neg_target = torch.zeros(n_s.shape[0])

        score = torch.cat([p_s, n_s])
        target = torch.cat([pos_target, neg_target])

        record[0, i], record[1, i], record[2, i] = auprc_auroc_ap(target, score)

    return record


print('model training ...')
for epoch in range(EPOCH_NUM):
    time_begin = time.time()

    z, loss = train()

    if epoch % 10 == 9:
        record_te = test(z)
        [auprc, auroc, ap] = record_te.sum(axis=1) / data.n_dd_et

        print('{:3d}   loss:{:0.4f}   auprc:{:0.4f}   auroc:{:0.4f}   ap@50:{:0.4f}    time:{:0.1f}'.format(epoch+1, loss.tolist(), auprc, auroc, ap, (time.time() - time_begin)))
    else:
        print('{:3d}   time:{:0.2f}'.format(epoch+1, (time.time() - time_begin)))

# # save model state
filepath_state = out_dir + '100ep.pth'
torch.save(model.state_dict(), filepath_state)
# # to restore
# # model.load_state_dict(torch.load(filepath_state))
# # model.eval()
#
# # save whole model
filepath_model = out_dir + '100ep_model.pb'
torch.save(model, filepath_model)
