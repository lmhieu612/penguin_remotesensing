import torch
import numpy as np
import torch.nn as nn
from torch.nn import init
import functools
from torch.optim import lr_scheduler
import torch.nn.functional as F




class PriorUnetGenerator(nn.Module):
    """Create a Unet-based generator"""

    def __init__(self, input_nc, output_nc, num_downs,num_downs_prior=4,inject_layer=4, ngf=64,prior_nf=16, norm_layer=nn.BatchNorm2d, use_dropout=False,gpu_ids=[],prior_nc=1,tanh=1):
        """Construct a Unet generator
        Parameters:
            input_nc (int)  -- the number of channels in input images
            output_nc (int) -- the number of channels in output images
            num_downs (int) -- the number of downsamplings in UNet. For example, # if |num_downs| == 7,
                                image of size 128x128 will become of size 1x1 # at the bottleneck
            ngf (int)       -- the number of filters in the last conv layer
            norm_layer      -- normalization layer

        We construct the U-Net from the innermost layer to the outermost layer.
        It is a recursive process.
        """
        super(PriorUnetGenerator, self).__init__()
        self.tanh=tanh
        self.gpu_ids=gpu_ids
        self.prior_branch = DownBranchGenerator(prior_nc, num_downs=num_downs_prior, ngf=prior_nf, norm_layer=norm_layer)
        prior_branch_param= [self.prior_branch.out_nc,inject_layer+1]

        # construct unet structure
        unet_block = PriorUnetSkipConnectionBlock(ngf * 8 , ngf * 8, input_nc=None, submodule=None, norm_layer=norm_layer, innermost=True,depth=num_downs,prior_branch_param=prior_branch_param)  # add the innermost layer
        for i in range(num_downs - 5):          # add intermediate layers with ngf * 8 filters
            unet_block = PriorUnetSkipConnectionBlock(ngf * 8, ngf * 8, input_nc=None, submodule=unet_block, norm_layer=norm_layer, use_dropout=use_dropout,depth=num_downs-i-1,prior_branch_param=prior_branch_param)
        # gradually reduce the number of filters from ngf * 8 to ngf
        unet_block = PriorUnetSkipConnectionBlock(ngf * 4, ngf * 8, input_nc=None, submodule=unet_block, norm_layer=norm_layer,depth=4,prior_branch_param=prior_branch_param)
        unet_block = PriorUnetSkipConnectionBlock(ngf * 2, ngf * 4, input_nc=None, submodule=unet_block, norm_layer=norm_layer,depth=3,prior_branch_param=prior_branch_param)
        unet_block = PriorUnetSkipConnectionBlock(ngf, ngf * 2, input_nc=None, submodule=unet_block, norm_layer=norm_layer,depth=2,prior_branch_param=prior_branch_param)
        self.model = PriorUnetSkipConnectionBlock(output_nc, ngf, input_nc=input_nc, submodule=unet_block, outermost=True, norm_layer=norm_layer,depth=1,prior_branch_param=prior_branch_param)  # add the outermost layer

    def forward(self, input,prior_input):
        """Alternative Branch forward """
        prior_feature = self.prior_branch.forward(prior_input)
        self.model.get_prior(prior_feature)
        """Standard forward"""
        X = self.model(input)
        return F.tanh(self.tanh*X)

class PriorUnetSkipConnectionBlock(nn.Module):
    """Defines the Unet submodule with skip connection.
        X -------------------identity----------------------
        |-- downsampling -- |submodule| -- upsampling --|
    """
    prior_feature = []
    def __init__(self, outer_nc, inner_nc, input_nc=None,
                 submodule=None, outermost=False, innermost=False, norm_layer=nn.BatchNorm2d, use_dropout=False,depth=1,prior_branch_param=None):
        """Construct a Unet submodule with skip connections.

        Parameters:
            outer_nc (int) -- the number of filters in the outer conv layer
            inner_nc (int) -- the number of filters in the inner conv layer
            input_nc (int) -- the number of channels in input images/features
            submodule (UnetSkipConnectionBlock) -- previously defined submodules
            outermost (bool)    -- if this module is the outermost module
            innermost (bool)    -- if this module is the innermost module
            norm_layer          -- normalization layer
            user_dropout (bool) -- if use dropout layers.
        """
        super(PriorUnetSkipConnectionBlock, self).__init__()
        self.merge_here = depth == prior_branch_param[1]
        self.merged_up_here = depth == prior_branch_param[1]-1
        self.depth= depth

        prior_nc = prior_branch_param[0]
        self.outermost = outermost
        self.innermost = innermost
        if type(norm_layer) == functools.partial:
            use_bias = norm_layer.func == nn.InstanceNorm2d
        else:
            use_bias = norm_layer == nn.InstanceNorm2d
        if input_nc is None:
            input_nc = outer_nc
        if self.merge_here:
            downconv = nn.Conv2d(input_nc + prior_branch_param[0], inner_nc, kernel_size=4,
                     stride=2, padding=1, bias=use_bias)
        else:
            downconv = nn.Conv2d(input_nc, inner_nc, kernel_size=4,
                             stride=2, padding=1, bias=use_bias)
        downrelu = nn.LeakyReLU(0.2, False)
        downnorm = norm_layer(inner_nc)
        uprelu = nn.ReLU(False)
        upnorm = norm_layer(outer_nc)

        if outermost:
            if self.merge_here:
                in_nc = inner_nc*2 + prior_nc
            else:
                in_nc = inner_nc*2
            
            upconv = nn.ConvTranspose2d(in_nc, outer_nc,
                                        kernel_size=4, stride=2,
                                        padding=1)
            down = [downconv]
            up = [uprelu, upconv]
            model = down + [submodule] + up
        elif innermost:
            if self.merge_here:
                in_nc = inner_nc 
            else:
                in_nc = inner_nc
            upconv = nn.ConvTranspose2d(in_nc, outer_nc,
                                        kernel_size=4, stride=2,
                                        padding=1, bias=use_bias)
            down = [downrelu, downconv]
            up = [uprelu, upconv, upnorm]
            model = down + up
        else:
            if self.merged_up_here:
                in_nc = inner_nc*2 + prior_nc
            else:
                in_nc = inner_nc*2
            upconv = nn.ConvTranspose2d(in_nc, outer_nc,
                                        kernel_size=4, stride=2,
                                        padding=1, bias=use_bias)
            down = [downrelu, downconv, downnorm]
            up = [uprelu, upconv, upnorm]

            if use_dropout:
                model = down + [submodule] + up + [nn.Dropout(0.5)]
            else:
                model = down + [submodule] + up

        self.model = nn.Sequential(*model)

    def get_prior(self,prior_feature):
        PriorUnetSkipConnectionBlock.prior_feature = prior_feature

    def forward(self, x):
        if self.merge_here:
            prior_feature = PriorUnetSkipConnectionBlock.prior_feature
            if prior_feature.shape[-1] != x.shape[-1] or prior_feature.shape[-2] !=x.shape[-2]:
                prior_feature = F.interpolate(prior_feature,size = [x.shape[-2],x.shape[-1]],mode='bilinear')
            x = torch.cat([x,prior_feature],1)
        if self.outermost:
            return self.model(x)
        else:   # add skip connections
            return torch.cat([x, self.model(x)], 1)

class DownBranchGenerator(nn.Module):
    """Create a single feed-forward branch similar to Unet encoder."""

    def __init__(self, input_nc, num_downs, ngf=64, norm_layer=nn.BatchNorm2d, use_dropout=False):



        super(DownBranchGenerator, self).__init__()
        unet_block = [DownBlock(input_nc, ngf, norm_layer=norm_layer)]  # add the outermost layer
        in_nc = ngf
        for i in range(num_downs-1):
            out_nc = ngf * (2**(np.amin([3,i])))
            block = [DownBlock(in_nc, out_nc, norm_layer=norm_layer)]
            in_nc = out_nc
            unet_block = unet_block + block
        self.model = nn.Sequential(*unet_block)
        self.out_nc = out_nc

    def forward(self, input):
        """Standard forward"""
        return self.model(input)

class DownBlock(nn.Module):

    def __init__(self, input_nc, output_nc, norm_layer=nn.BatchNorm2d, use_dropout=False):
        super(DownBlock, self).__init__()
        if type(norm_layer) == functools.partial:
            use_bias = norm_layer.func == nn.InstanceNorm2d
        else:
            use_bias = norm_layer == nn.InstanceNorm2d
        downconv = nn.Conv2d(input_nc, output_nc, kernel_size=4,
                             stride=2, padding=1, bias=use_bias)
        downrelu = nn.LeakyReLU(0.2,False)
        downnorm = norm_layer(output_nc)
        down = [downrelu, downconv,downnorm]
        if use_dropout:
            model = down + [nn.Dropout(0.5)]
        else:
            model = down 
        self.model = nn.Sequential(*model)

    def forward(self, x):
        return self.model(x)

if __name__=='__main__':
    norm_layer = functools.partial(nn.InstanceNorm2d, affine=False)
    a = PriorUnetGenerator(3,1,num_downs=8,num_downs_prior=6,inject_layer=6,ngf=64,prior_nf=12,norm_layer=norm_layer)    
    
    inp = torch.rand(1,3,256,256)
    inp2 = -torch.ones(1,1,256,256)
    #t_model ='/nfs/bigdisk/hieule/checkpoints_CVPR19W/v3weakly_unetprior_bs96_idepth6_pdepth6_None/500_net_G.pth'
    #t_model ='/nfs/bigdisk/hieule/checkpoints_CVPR19W/v3weakly_unetprior_bs96_idepth6_pdepth6_pnf_test/latest_net_G.pth'
#    t_model ='./test.pth'
#    a.cuda(0)
#    torch.save(a.cpu().state_dict(),t_model)
    #a.load_state_dict(torch.load(t_model))
