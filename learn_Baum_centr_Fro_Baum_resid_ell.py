import torch
import torch.nn as nn
import numpy as np
from  scipy.ndimage import zoom as imzoom
import sys
import os
import math
from PIL import Image
from matplotlib import mlab
import matplotlib.pyplot as plt
import numpy as np
from pytorch_sift import SIFTNet
import torchvision.datasets as dset
import torchvision.transforms as transforms
from torch.autograd import Variable
import torch.backends.cudnn as cudnn
import torch.optim as optim
from tqdm import tqdm
import torch.nn.functional as F

USE_CUDA = True

LOG_DIR = 'log_snaps'
BASE_LR = 0.01
start = 0
end = 20
n_epochs = end - start
from SpatialTransformer2D import SpatialTransformer2d
from HardNet import HardNet
#hardnet = HardNet()
#checkpoint = torch.load('HardNetLib.pth')
#hardnet.load_state_dict(checkpoint['state_dict'])
from HandCraftedModules import AffineShapeEstimator
from Utils import CircularGaussKernel
from SparseImgRepresenter import ScaleSpaceAffinePatchExtractor
from LAF import denormalizeLAFs, LAFs2ell, abc2A
from ReprojectonStuff import get_GT_correspondence_indexes_Fro,get_GT_correspondence_indexes,get_GT_correspondence_indexes_Fro_and_center
class BaumResNetEll(nn.Module):
    """HardNet model definition
    """
    def __init__(self, PS = 16):
        super(BaumResNetEll, self).__init__()

        self.features = nn.Sequential(
            nn.Conv2d(1, 16, kernel_size=3, padding=1, bias = False),
            nn.BatchNorm2d(16, affine=False),
            nn.ReLU(),
            nn.Conv2d(16, 32, kernel_size=3, stride=2, padding=1, bias = False),
            nn.BatchNorm2d(32, affine=False),
            nn.ReLU(),
            nn.Conv2d(32, 32, kernel_size=3, stride=1, padding=1, bias = False),
            nn.BatchNorm2d(32, affine=False),
            nn.ReLU(),
            nn.Conv2d(32, 64, kernel_size=3, stride=2,padding=1, bias = False),
            nn.BatchNorm2d(64, affine=False),
            nn.ReLU(),
            nn.Dropout(0.1),
            nn.Conv2d(64, 3, kernel_size=4, bias = True),
            nn.Tanh()
        )
        self.PS = PS
        self.features.apply(weights_init)
        self.gx =  nn.Conv2d(1, 1, kernel_size=(1,3), bias = False)
        self.gx.weight.data = torch.from_numpy(np.array([[[[0.5, 0, -0.5]]]], dtype=np.float32))

        self.gy =  nn.Conv2d(1, 1, kernel_size=(3,1), bias = False)
        self.gy.weight.data = torch.from_numpy(np.array([[[[0.5], [0], [-0.5]]]], dtype=np.float32))

        self.gxx =  nn.Conv2d(1, 1, kernel_size=(1,3),bias = False)
        self.gxx.weight.data = torch.from_numpy(np.array([[[[1.0, -2.0, 1.0]]]], dtype=np.float32))
        
        self.gyy =  nn.Conv2d(1, 1, kernel_size=(3,1), bias = False)
        self.gyy.weight.data = torch.from_numpy(np.array([[[[1.0], [-2.0], [1.0]]]], dtype=np.float32))
        self.gk = torch.from_numpy(CircularGaussKernel(kernlen=PS, circ_zeros = False).astype(np.float32))
        self.gk = Variable(self.gk, requires_grad=False)

        return
    def invSqrt(self,a,b,c):
        eps = 1e-12
        mask = (b != 0).float()
        r1 = mask * (c - a) / (2. * b + eps)
        t1 = torch.sign(r1) / (torch.abs(r1) + torch.sqrt(1. + r1*r1));
        r = 1.0 / torch.sqrt( 1. + t1*t1)
        t = t1*r;
        
        r = r * mask + 1.0 * (1.0 - mask);
        t = t * mask;
        
        x = 1. / torch.sqrt( r*r*a - 2*r*t*b + t*t*c)
        z = 1. / torch.sqrt( t*t*a + 2*r*t*b + r*r*c)
        
        d = torch.sqrt( x * z)
        
        x = x / d
        z = z / d
        
        l1 = torch.max(x,z)
        l2 = torch.min(x,z)
        
        new_a = r*r*x + t*t*z
        new_b = -r*t*x + t*r*z
        new_c = t*t*x + r*r *z

        return l1,l2, new_a, new_b, new_c
    def input_norm(self,x):
        flat = x.view(x.size(0), -1)
        mp = torch.mean(flat, dim=1)
        sp = torch.std(flat, dim=1) + 1e-7
        return (x - mp.unsqueeze(-1).unsqueeze(-1).unsqueeze(-1).expand_as(x)) / sp.unsqueeze(-1).unsqueeze(-1).unsqueeze(1).expand_as(x)

    def forward(self, x):
        abc = self.features(self.input_norm(x))
        a11 = 0.001 * (abc[:,0,:,:].contiguous())
        b11 = 0.001 * (abc[:,1,:,:].contiguous())
        c11 = 0.001 * (abc[:,2,:,:].contiguous())
        if x.is_cuda:
            self.gk = self.gk.cuda()
        gx = self.gx(F.pad(x, (1,1,0, 0), 'replicate'))
        gy = self.gy(F.pad(x, (0,0, 1,1), 'replicate'))
        a1 = (gx*gx * self.gk.unsqueeze(0).unsqueeze(0).expand_as(gx)).view(x.size(0),-1).mean(dim=1)
        b1 = (gx*gy * self.gk.unsqueeze(0).unsqueeze(0).expand_as(gx)).view(x.size(0),-1).mean(dim=1)
        c1 = (gy*gy * self.gk.unsqueeze(0).unsqueeze(0).expand_as(gx)).view(x.size(0),-1).mean(dim=1)
        l1, l2, a, b, c = self.invSqrt(a1+a11.view(-1),b1+b11.view(-1),c1+c11.view(-1))
        rat1 = l1/l2
        mask = (torch.abs(rat1) <= 6.).float().view(-1);
        out = torch.cat([torch.cat([a.unsqueeze(-1).unsqueeze(-1), b.unsqueeze(-1).unsqueeze(-1)], dim = 2),
                                        torch.cat([b.unsqueeze(-1).unsqueeze(-1), c.unsqueeze(-1).unsqueeze(-1)], dim = 2)],
                                        dim = 1)
        #a = a * mask + 1. * (1.- mask)
        #b = b * mask + 0. * (1.- mask)
        #c = c * mask + 1. * (1.- mask)
        return out, mask

def weights_init(m):
    if isinstance(m, nn.Conv2d):
        nn.init.orthogonal(m.weight.data, gain=1.0)
        try:
            nn.init.constant(m.bias.data, 0.01)
        except:
            pass
    return
    
def adjust_learning_rate(optimizer):
    """Updates the learning rate given the learning rate decay.
    The routine has been implemented according to the original Lua SGD optimizer
    """
    n_triplets = 116*5.
    n_epochs = 10.
    for group in optimizer.param_groups:
        if 'step' not in group:
            group['step'] = 0.
        else:
            group['step'] += 1.
        group['lr'] =  BASE_LR *  (1.0 - float(group['step']) * float(1.0) / (n_triplets * float(n_epochs)))
    return

def create_optimizer(model, new_lr, wd):
    # setup optimizer
    optimizer = optim.SGD(model.parameters(), lr=new_lr,
                          momentum=0.5, dampening=0.5,
                          weight_decay=wd)
    return optimizer

def create_loaders(load_random_triplets = False):

    kwargs = {'num_workers': 2, 'pin_memory': True} if True else {}

    transform = transforms.Compose([
            transforms.ToTensor()])
    #        transforms.Normalize((args.mean_image,), (args.std_image,))])

    train_loader = torch.utils.data.DataLoader(
            dset.HPatchesSeq('/home/old-ufo/storage/learned_detector/dataset/', 'b',
                             train=True, transform=None,
                             download=True), batch_size = 1,
        shuffle = True, **kwargs)

    test_loader = torch.utils.data.DataLoader(
            dset.HPatchesSeq('/home/old-ufo/storage/learned_detector/dataset/', 'b',
                             train=False, transform=None,
                             download=True), batch_size = 1,
        shuffle = False, **kwargs)

    return train_loader, test_loader

def train(train_loader, model, optimizer, epoch, cuda = True):
    # switch to train mode
    model.train()
    log_interval = 1
    total_loss = 0
    spatial_only = True
    pbar = enumerate(train_loader)
    for batch_idx, data in pbar:
        print 'Batch idx', batch_idx
        #print model.detector.shift_net[0].weight.data.cpu().numpy()
        img1, img2, H1to2  = data
        #if np.abs(np.sum(H.numpy()) - 3.0) > 0.01:
        #    continue
        H1to2 = H1to2.squeeze(0)
        if (img1.size(3) *img1.size(4)   > 1340*1000):
            print img1.shape, ' too big, skipping'
            continue
        img1 = img1.float().squeeze(0)
        #img1 = img1 - img1.mean()
        #img1 = img1 / 50.#(img1.std() + 1e-8)
        img2 = img2.float().squeeze(0)
        #img2 = img2 - img2.mean()
        #img2 = img2 / 50.#(img2.std() + 1e-8)
        if cuda:
            img1, img2, H1to2 = img1.cuda(), img2.cuda(), H1to2.cuda()
        img1, img2, H1to2 = Variable(img1, requires_grad = False), Variable(img2, requires_grad = False), Variable(H1to2, requires_grad = False)
        LAFs1, aff_norm_patches1, resp1, pyr1 = HA(img1)
        LAFs2, aff_norm_patches2, resp2, pyr2 = HA(img2)
        if (len(LAFs1) == 0) or (len(LAFs2) == 0):
            optimizer.zero_grad()
            continue
        fro_dists, idxs_in1, idxs_in2 = get_GT_correspondence_indexes_Fro_and_center(LAFs1,LAFs2, H1to2,  dist_threshold = 3., 
                                                                             center_dist_th = 7.0,
                                                                            skip_center_in_Fro = True,
                                                                            do_up_is_up = True);
        print LAFs1[0,:,:] 
        if  len(fro_dists.size()) == 0:
            optimizer.zero_grad()
            print 'skip'
            continue
        loss = fro_dists.mean()
        total_loss += loss.data.cpu().numpy()[0]
        #patch_dist = torch.mean((aff_norm_patches1[idxs_in1.data.long(),:,:,:] - aff_norm_patches2[idxs_in2.data.long(), :,:,:]) **2)
        print loss.data.cpu().numpy()[0]#, patch_dist.data.cpu().numpy()[0]
        #loss += patch_dist
        optimizer.zero_grad()
        loss.backward()
        optimizer.step()
        #adjust_learning_rate(optimizer)
        print epoch,batch_idx, loss.data.cpu().numpy()[0], idxs_in1.shape

    print 'Train total loss:', total_loss / float(batch_idx+1)
    torch.save({'epoch': epoch + 1, 'state_dict': model.state_dict()},
               '{}/ResBaumEll_checkpoint_{}.pth'.format(LOG_DIR, epoch))

def test(test_loader, model, cuda = True):
    # switch to train mode
    model.eval()
    log_interval = 1
    pbar = enumerate(test_loader)
    total_loss = 0
    total_feats = 0
    for batch_idx, data in pbar:
        print 'Batch idx', batch_idx
        img1, img2, H1to2  = data
        if (img1.size(3) *img1.size(4)   > 1500*1200):
            print img1.shape, ' too big, skipping'
            continue
        H1to2 = H1to2.squeeze(0)
        img1 = img1.float().squeeze(0)
        img2 = img2.float().squeeze(0)
        if cuda:
            img1, img2, H1to2 = img1.cuda(), img2.cuda(), H1to2.cuda()
        img1, img2, H1to2 = Variable(img1, volatile = True), Variable(img2, volatile = True), Variable(H1to2, volatile = True)
        LAFs1, aff_norm_patches1, resp1, pyr1 = HA(img1)
        LAFs2, aff_norm_patches2, resp2, pyr2 = HA(img2)
        if (len(LAFs1) == 0) or (len(LAFs2) == 0):
            continue
        fro_dists, idxs_in1, idxs_in2 = get_GT_correspondence_indexes_Fro_and_center(LAFs1,LAFs2, H1to2, 
                                                                                     dist_threshold = 3.,
                                                                             center_dist_th = 7.0,
                                                                            skip_center_in_Fro = True,
                                                                            do_up_is_up = True);
        print LAFs1[0,:,:] 
        if  len(fro_dists.size()) == 0:
            print 'skip'
            continue
        loss = fro_dists.mean()
        total_feats += fro_dists.size(0)
        total_loss += loss.data.cpu().numpy()[0]
        print 'test img', batch_idx, loss.data.cpu().numpy()[0], fro_dists.size(0)
    print 'Total loss:', total_loss / float(batch_idx+1), 'features', float(total_feats) / float(batch_idx+1)

train_loader, test_loader = create_loaders()

HA = ScaleSpaceAffinePatchExtractor( mrSize = 3.0, num_features = 350, border = 5, num_Baum_iters = 1, AffNet = BaumResNetEll())


model = HA
if USE_CUDA:
    model = model.cuda()

optimizer1 = create_optimizer(model.AffNet.features, BASE_LR, 5e-5)


test(test_loader, model, cuda = USE_CUDA)
for epoch in range(n_epochs):
    print 'epoch', epoch
    if USE_CUDA:
        model = model.cuda()
    train(train_loader, model, optimizer1, epoch, cuda = USE_CUDA)
    test(test_loader, model, cuda = USE_CUDA)
