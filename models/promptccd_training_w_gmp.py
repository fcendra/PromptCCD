# -----------------------------------------------------------------------------
# PromptCCD model training with Gaussian Mixture Prompt (GMP), known K
# -----------------------------------------------------------------------------
import os
from tqdm import tqdm, trange

import torch
from torch.nn import functional as F
from torch.optim import SGD, lr_scheduler

from models import vision_transformer as vits
from models.promptccd_utils.vision_transformer import vit_base_patch16_224_dino
from models.promptccd_utils.vision_transformer_v2 import vit_base_patch14_dinov2
from models.promptccd_utils.gmp_known_K import GMMPrompt
from models.sskmeans import eval_kmeans, eval_kmeans_semi_sup
from util.util import info
from util.eval_util import AverageMeter

device = torch.device('cuda:0')


class PromptCCD_Model:
    def __init__(self, args, model, stage_i):
        super().__init__()
        self.args = args
        self.stage_i = stage_i
        
        if model == None:
            self.model, self.projection_head = get_vit_model(args)
            self.gmm_prompt = GMMPrompt(args, int(args.labelled_data))   
        else:
            (self.model, self.projection_head) = model
            print(f'Loading best model and projection head state dict from stage {self.stage_i - 1}...')
            self.model.load_state_dict(torch.load(os.path.join(args.save_path, 'model', f'{args.ccd_model}_stage_{self.stage_i - 1}_model_best.pt')))
            self.projection_head.load_state_dict(torch.load(os.path.join(args.save_path, 'model', f'{args.ccd_model}_stage_{self.stage_i - 1}_proj_head_best.pt')))
            K = int(args.labelled_data + (stage_i * ((args.classes - args.labelled_data) // args.n_stage)))
            if args.set_k_half: K //= 2
            self.gmm_prompt = GMMPrompt(args, K)

        self.model_path = os.path.join(args.save_path, 'model')

    def fit(self, train_loader, val_loader):
        optimizer = SGD(
            list(self.projection_head.parameters()) + list(self.model.parameters()), 
            lr=self.args.base_lr, 
            momentum=self.args.momentum,
            weight_decay=self.args.weight_decay
        )
        
        exp_lr_scheduler = lr_scheduler.CosineAnnealingLR(
            optimizer,
            T_max=self.args.epochs,
            eta_min=self.args.base_lr * 1e-3,
        )

        sup_con_crit = SupConLoss()
        best_test_acc_lab = 0
        res = None

        for epoch in trange(self.args.epochs, desc='Epochs', bar_format="{desc}{percentage:3.0f}%|{bar}{r_bar}", ncols=80):

            loss_record = AverageMeter()
            train_acc_record = AverageMeter()

            self.projection_head.train()
            self.model.train(True)
            
            if epoch % self.args.fit_gmm_every_n_epoch == 0:
                self.gmm_prompt.fit(self.args, self.model, train_loader['default'], self.stage_i)

            for batch in tqdm(train_loader['contrast'], desc='Batches', leave=False, bar_format="{desc}{percentage:3.0f}%|{bar}{r_bar}", ncols=80):

                images, class_labels, _, mask_lab = batch
                mask_lab = mask_lab[:, 0]

                class_labels, mask_lab = class_labels.to(device), mask_lab.to(device).bool()
                images = torch.cat(images, dim=0)
                images = images.to(device)

                if epoch >= self.args.fit_gmm_every_n_epoch:
                    with torch.no_grad():
                        batch_feats = self.model(images, task_id=self.stage_i, res=None)['x'][:, 0]
                    res = self.gmm_prompt.predict(batch_feats)

                # Extract features with base model
                output = self.model(images, task_id=self.stage_i, res=res)
                features = output['x'][:, 0] 

                # Pass features through projection head
                features = self.projection_head(features)

                # L2-normalize features
                features = torch.nn.functional.normalize(features, dim=-1)

                # Choose which instances to run the contrastive loss on
                if self.args.contrast_unlabel_only:
                    # Contrastive loss only on unlabelled instances
                    f1, f2 = [f[~mask_lab] for f in features.chunk(2)]
                    con_feats = torch.cat([f1, f2], dim=0)
                else:
                    # Contrastive loss for all examples
                    con_feats = features

                contrastive_logits, contrastive_labels = info_nce_logits(features=con_feats, args=self.args)
                contrastive_loss = torch.nn.CrossEntropyLoss()(contrastive_logits, contrastive_labels)

                # Supervised contrastive loss, during the initial learning on labelled data
                if self.args.sup_con_weight[self.stage_i] > 0:
                    f1, f2 = [f[mask_lab] for f in features.chunk(2)]
                    sup_con_feats = torch.cat([f1.unsqueeze(1), f2.unsqueeze(1)], dim=1)
                    sup_con_labels = class_labels[mask_lab]
                    sup_con_loss = sup_con_crit(sup_con_feats, labels=sup_con_labels)
                else:
                    sup_con_loss = 0

                # Total loss
                loss = ((1 - self.args.sup_con_weight[self.stage_i]) * contrastive_loss) + (self.args.sup_con_weight[self.stage_i] * sup_con_loss)

                # Train acc
                _, pred = contrastive_logits.max(1)
                acc = (pred == contrastive_labels).float().mean().item()
                train_acc_record.update(acc, pred.size(0))

                loss_record.update(loss.item(), class_labels.size(0))
                optimizer.zero_grad()
                loss.backward()
                optimizer.step()

            # Step schedule
            exp_lr_scheduler.step()

            if epoch % self.args.eval_every_n_epoch == 0:
                with torch.no_grad():
                    # we only evaluate on the 'old' classes, to mimic the CCD setting
                    _, old_acc_test, _ = eval_kmeans(
                        args=self.args, 
                        model=(self.model, self.gmm_prompt),
                        val_loader=val_loader,
                        stage_i=self.stage_i,
                        epoch=epoch,
                    ) 

                # ----------------
                # LOG
                # ----------------                
                torch.save(self.model.state_dict(), os.path.join(self.model_path, f'{self.args.ccd_model}_stage_{self.stage_i}_model.pt'))
                torch.save(self.projection_head.state_dict(), os.path.join(self.model_path,f'{self.args.ccd_model}_stage_{self.stage_i}_model_proj_head.pt'))

                if old_acc_test > best_test_acc_lab:
                    torch.save(self.model.state_dict(), os.path.join(self.model_path, f'{self.args.ccd_model}_stage_{self.stage_i}_model_best.pt'))
                    torch.save(self.projection_head.state_dict(), os.path.join(self.model_path, f'{self.args.ccd_model}_stage_{self.stage_i}_proj_head_best.pt'))
                    best_test_acc_lab = old_acc_test

        return self.model, self.projection_head

    def eval(self, test_loader):
        all_acc, old_acc, new_acc = eval_kmeans_semi_sup(
            args=self.args, 
            model=(self.model, self.gmm_prompt),
            data_loader=test_loader, 
            stage_i=self.stage_i, 
            K=None,
        )


class SupConLoss(torch.nn.Module):
    """Supervised Contrastive Learning: https://arxiv.org/pdf/2004.11362.pdf.
    It also supports the unsupervised contrastive loss in SimCLR
    From: https://github.com/HobbitLong/SupContrast"""
    def __init__(self, temperature=0.07, contrast_mode='all',
                 base_temperature=0.07):
        super(SupConLoss, self).__init__()
        self.temperature = temperature
        self.contrast_mode = contrast_mode
        self.base_temperature = base_temperature

    def forward(self, features, labels=None, mask=None):
        """Compute loss for model. If both `labels` and `mask` are None,
        it degenerates to SimCLR unsupervised loss:
        https://arxiv.org/pdf/2002.05709.pdf
        Args:
            features: hidden vector of shape [bsz, n_views, ...].
            labels: ground truth of shape [bsz].
            mask: contrastive mask of shape [bsz, bsz], mask_{i,j}=1 if sample j
                has the same class as sample i. Can be asymmetric.
        Returns:
            A loss scalar.
        """

        device = (torch.device('cuda')
                  if features.is_cuda
                  else torch.device('cpu'))

        if len(features.shape) < 3:
            raise ValueError('`features` needs to be [bsz, n_views, ...],'
                             'at least 3 dimensions are required')
        if len(features.shape) > 3:
            features = features.view(features.shape[0], features.shape[1], -1)

        batch_size = features.shape[0]
        if labels is not None and mask is not None:
            raise ValueError('Cannot define both `labels` and `mask`')
        elif labels is None and mask is None:
            mask = torch.eye(batch_size, dtype=torch.float32).to(device)
        elif labels is not None:
            labels = labels.contiguous().view(-1, 1)
            if labels.shape[0] != batch_size:
                raise ValueError('Num of labels does not match num of features')
            mask = torch.eq(labels, labels.T).float().to(device)
        else:
            mask = mask.float().to(device)

        contrast_count = features.shape[1]
        contrast_feature = torch.cat(torch.unbind(features, dim=1), dim=0)
        if self.contrast_mode == 'one':
            anchor_feature = features[:, 0]
            anchor_count = 1
        elif self.contrast_mode == 'all':
            anchor_feature = contrast_feature
            anchor_count = contrast_count
        else:
            raise ValueError('Unknown mode: {}'.format(self.contrast_mode))

        # compute logits
        anchor_dot_contrast = torch.div(
            torch.matmul(anchor_feature, contrast_feature.T),
            self.temperature)

        # for numerical stability
        logits_max, _ = torch.max(anchor_dot_contrast, dim=1, keepdim=True)
        logits = anchor_dot_contrast - logits_max.detach()

        # tile mask
        mask = mask.repeat(anchor_count, contrast_count)
        # mask-out self-contrast cases
        logits_mask = torch.scatter(
            torch.ones_like(mask),
            1,
            torch.arange(batch_size * anchor_count).view(-1, 1).to(device),
            0
        )
        mask = mask * logits_mask

        # compute log_prob
        exp_logits = torch.exp(logits) * logits_mask
        log_prob = logits - torch.log(exp_logits.sum(1, keepdim=True))

        # compute mean of log-likelihood over positive
        mean_log_prob_pos = (mask * log_prob).sum(1) / mask.sum(1)

        # loss
        loss = - (self.temperature / self.base_temperature) * mean_log_prob_pos
        loss = loss.view(anchor_count, batch_size).mean()

        return loss


def info_nce_logits(features, args):

    b_ = 0.5 * int(features.size(0))

    labels = torch.cat([torch.arange(b_) for i in range(args.n_views)], dim=0)
    labels = (labels.unsqueeze(0) == labels.unsqueeze(1)).float()
    labels = labels.to(device)

    features = F.normalize(features, dim=1)

    similarity_matrix = torch.matmul(features, features.T)

    # discard the main diagonal from both: labels and similarities matrix
    mask = torch.eye(labels.shape[0], dtype=torch.bool).to(device)
    labels = labels[~mask].view(labels.shape[0], -1)
    similarity_matrix = similarity_matrix[~mask].view(similarity_matrix.shape[0], -1)
    # assert similarity_matrix.shape == labels.shape

    # select and combine multiple positives
    positives = similarity_matrix[labels.bool()].view(labels.shape[0], -1)

    # select only the negatives the negatives
    negatives = similarity_matrix[~labels.bool()].view(similarity_matrix.shape[0], -1)

    logits = torch.cat([positives, negatives], dim=1)
    labels = torch.zeros(logits.shape[0], dtype=torch.long).to(device)

    logits = logits / args.temperature
    return logits, labels


def get_vit_model(args):

    args.interpolation = 3
    args.crop_pct = 0.875
    if args.use_dinov2:
        model = vit_base_patch14_dinov2(
            pretrained=True, 
            num_classes=0, 
            prompt_pool=args.prompt_pool,
            top_k=args.top_k,
            img_size=args.input_size,
            patch_size=14,
            pretrained_strict=False,
        )
    else:
        model = vit_base_patch16_224_dino(
            pretrained=True, 
            num_classes=0, 
            embedding_key=args.embedding_key,
            prompt_pool=args.prompt_pool,
            top_k=args.top_k,
            head_type=args.head_type,
        )
    
    model.to(device)

    # NOTE: Hardcoded image size as we do not finetune the entire ViT model
    args.image_size = 224
    args.feat_dim = 768
    args.num_mlp_layers = 3
    args.mlp_out_dim = 65536

    # ----------------------
    # HOW MUCH OF BASE MODEL TO FINETUNE
    # ----------------------
    for n, p in model.named_parameters():
        if n.startswith(tuple(args.freeze)):
            p.requires_grad = False

    # Only finetune layers from block 'args.grad_from_block' onwards
    for name, m in model.named_parameters():
        if 'block' in name:
            block_num = int(name.split('.')[1])
            if block_num >= args.grad_from_block:
                m.requires_grad = True

    projection_head = vits.__dict__['DINOHead'](in_dim=args.feat_dim,
                               out_dim=args.mlp_out_dim, nlayers=args.num_mlp_layers)

    projection_head.to(device)

    return model, projection_head
