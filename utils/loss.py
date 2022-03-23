import torch
import torch.nn as nn
import torch.nn.functional as F


class FocalLoss(nn.Module):
    def __init__(self, alpha=0.25, gamma=2.0, ignore_index=-100, reduction='mean', label_smoothing=0.0):
        super(FocalLoss, self).__init__()
        self.alpha = alpha  # positive/negative 샘플의 중요도에 대한 균형
        self.gamma = gamma  # easy 샘플의 loss 감소 정도를 조절
        self.ignore_index = ignore_index
        self.reduction = reduction
        self.label_smoothing = label_smoothing

    def forward(self, input: torch.Tensor, target: torch.Tensor) -> torch.Tensor:
        ce_loss = F.cross_entropy(input, target, ignore_index=self.ignore_index,
                                  reduction='none', label_smoothing=self.label_smoothing)
        pt = torch.exp(-ce_loss)
        focal_loss = self.alpha * (1 - pt) ** self.gamma * ce_loss

        if self.reduction == 'mean':
            return focal_loss.mean()
        elif self.reduction == 'sum':
            return focal_loss.sum()
        elif self.reduction == 'none':
            return focal_loss
        else:
            raise ValueError('Wrong reduction.')


if __name__ == '__main__':
    input = torch.randn(2, 5)
    target = torch.randint(5, (2,), dtype=torch.int64)
    loss1 = F.cross_entropy(input, target, reduction='none')
    loss2 = FocalLoss(reduction='none')(input, target)
    print(loss1)
    print(loss2)
