import os.path
import argparse
from math import sqrt, exp
import torch as th
from data import MTDataset, MTDataLoader, Vocab
from transformer import Transformer
from tqdm import tqdm
import pdb

# 3:2: 31.77, 1:1: 30.31
def load_data(src_lang, tgt_lang, cached_folder="assignment2/data", overwrite=False):
    """Load data (and cache to file)"""
    cached_file = os.path.join(cached_folder, f"{src_lang}-{tgt_lang}.pt")
    if not os.path.isfile(cached_file) or overwrite:
        base_folder = os.path.join(
            "assignment2",
            "data",
            f"{src_lang}_{tgt_lang}" if src_lang == 'en' else f"{tgt_lang}_{src_lang}"
        )
        train_prefix = os.path.join(
            base_folder,
            f"{src_lang}{tgt_lang}_parallel.bpe.train" if src_lang == "en" \
                else f"{tgt_lang}{src_lang}_parallel.bpe.train"
        )
        dev_prefix = os.path.join(
            base_folder,
            f"{src_lang}{tgt_lang}_parallel.bpe.dev" if src_lang == "en" \
                else f"{tgt_lang}{src_lang}_parallel.bpe.dev"
        )
        vocab = Vocab.from_data_files(
            f"{train_prefix}.{src_lang}",
            f"{train_prefix}.{tgt_lang}",
        )
        train = MTDataset(vocab, train_prefix,
                          src_lang=src_lang, tgt_lang=tgt_lang)
        valid = MTDataset(vocab, dev_prefix,
                          src_lang=src_lang, tgt_lang=tgt_lang)
        th.save([vocab, train, valid], cached_file)
    # Load cached dataset
    return th.load(cached_file)


def get_args():
    parser = argparse.ArgumentParser("Train an MT model")
    # General params
    parser.add_argument("--seed", type=int, default=11731)
    parser.add_argument("--src", type=str, default="en", choices=["af", "ts", "nso","en"])
    parser.add_argument("--tgt", type=str, default="af",
                        choices=["af", "ts", "nso", "en"])
    parser.add_argument("--model-file", type=str, default="model.pt")
    parser.add_argument("--overwrite-model", action="store_true")
    parser.add_argument("--cuda", action="store_true")
    parser.add_argument("--validate-only", action="store_true")
    # Model parameters
    parser.add_argument("--n-layers", type=int, default=4)
    parser.add_argument("--n-heads", type=int, default=4)
    parser.add_argument("--embed-dim", type=int, default=512)
    parser.add_argument("--hidden-dim", type=int, default=512)
    parser.add_argument("--dropout", type=float, default=0.1)
    parser.add_argument("--word-dropout", type=float, default=0.1)
    # Optimization parameters
    parser.add_argument("--n-epochs", type=int, default=15)
    parser.add_argument("--lr", type=float, default=4e-2)
    parser.add_argument("--lr-decay", type=float, default=0.8)
    parser.add_argument("--inverse-sqrt-schedule", action="store_true")
    parser.add_argument("--clip-grad", type=float, default=1.0)
    parser.add_argument("--tokens-per-batch", type=int, default=8000)
    parser.add_argument("--samples-per-batch", type=int, default=128)
    parser.add_argument("--label-smoothing", type=float, default=0.1)
    return parser.parse_args()


def move_to_device(tensors, device):
    return [tensor.to(device) for tensor in tensors]


def inverse_sqrt_schedule(warmup, lr0):
    """Inverse sqrt learning rate schedule with warmup"""
    step = 0
    # Trick for allowing warmup of 0
    warmup = max(warmup, 0.01)
    while True:
        scale = min(1/sqrt(step+1e-20), step/sqrt(warmup**3))
        step += 1
        yield lr0 * scale

# reference from https://github.com/OpenNMT/OpenNMT-py/blob/e8622eb5c6117269bb3accd8eb6f66282b5e67d9/onmt/utils/loss.py#L186
class LabelSmoothingLoss(th.nn.Module):
    def __init__(self, label_smoothing, target_vocab_size, ignore_index=-1):
        self.ignore_index = ignore_index
        super(LabelSmoothingLoss, self).__init__()
        # label_smoothing is a small value
        smoothing_value = label_smoothing / (target_vocab_size - 1)
        one_hot = th.full((target_vocab_size, ), smoothing_value)
        self.register_buffer("one_hot", one_hot.unsqueeze(0))

        self.confidence = 1 - label_smoothing

    def forward(self, output, target):
        """
        output (FloatTensor): (batch_size x sentence_len) x n_classes
        target (LongTensor): (batch_size x sentence_len)
        """
        model_prob = self.one_hot.repeat(target.size(0), 1)
        model_prob.scatter_(1, target.unsqueeze(1), self.confidence)
        model_prob.masked_fill_((target == self.ignore_index).unsqueeze(1), 0)

        return th.nn.functional.kl_div(output, model_prob, reduction='sum')



def train_epoch(model, optim, dataloader, criterion, lr_schedule=None, clip_grad=5.0):
    # Model device
    device = list(model.parameters())[0].device
    # Iterate over batches
    itr = tqdm(dataloader)
    for batch in itr:
        optim.zero_grad()
        itr.total = len(dataloader)
        # Cast input to device
        batch = move_to_device(batch[2:], device)
        # Various inputs
        src_tokens, src_mask, tgt_tokens, tgt_mask = batch
        # Get log probs
        log_p = model(src_tokens, tgt_tokens[:-1], src_mask)
        # Negative log likelihood of the target tokens
        # (this selects log_p[i, b, tgt_tokens[i+1, b]]
        # for each batch b, position i)
        nll = criterion(
            log_p.view(-1, log_p.size(-1)),
            tgt_tokens[1:].view(-1),
        )
        nll /= sum(tgt_tokens[1:].view(-1)!=0)
        # nll = th.nn.functional.nll_loss(
        #     # Log probabilities (flattened to (l*b) x V)
        #     log_p.view(-1, log_p.size(-1)),
        #     # Target tokens (we start from the 1st real token, ignoring <sos>)
        #     tgt_tokens[1:].view(-1),
        #     # Don't compute the nll of padding tokens
        #     ignore_index=model.vocab["<pad>"],
        #     # Take the average
        #     reduction="mean",
        # )
        # Perplexity (for logging)
        ppl = th.exp(nll).item()
        # Backprop
        nll.backward()
        # Adjust learning rate with schedule
        if lr_schedule is not None:
            learning_rate = next(lr_schedule)
            for param_group in optim.param_groups:
                param_group["lr"] = learning_rate
        # Gradient clipping
        if clip_grad > 0:
            th.nn.utils.clip_grad_norm_(model.parameters(), clip_grad)
        # Optimizer step
        optim.step()
        # Update stats
        itr.set_postfix(loss=f"{nll.item():.3f}", ppl=f"{ppl:.2f}")


def evaluate_ppl(model, dataloader, epoch):
    model.eval()
    # Model device
    device = list(model.parameters())[0].device
    # total tokens
    tot_tokens = tot_nll = 0
    # Iterate over batches
    bleu = 0
    translated = []
    ground_truth = []
    for batch in tqdm(dataloader):
        # Cast input to device
        src_txt, tgt_txt = batch[0], batch[1]
        batch = move_to_device(batch[2:], device)
        # Various inputs
        src_tokens, src_mask, tgt_tokens, tgt_mask = batch
        with th.no_grad():
            # Get log probs
            log_p = model(src_tokens, tgt_tokens[:-1], src_mask)
            # Negative log likelihood of the target tokens
            # (this selects log_p[i, b, tgt_tokens[i+1, b]]
            # for each batch b, position i)
            nll = th.nn.functional.nll_loss(
                # Log probabilities (flattened to (l*b) x V)
                log_p.view(-1, log_p.size(-1)),
                # Target tokens (we start from the 1st real token)
                tgt_tokens[1:].view(-1),
                # Don't compute the nll of padding tokens
                ignore_index=model.vocab["<pad>"],
                # Take the average
                reduction="sum",
            )
            # Number of tokens (ignoring <sos> and <pad>)
            n_sos = tgt_tokens.eq(model.vocab["<sos>"]).float().sum().item()
            n_pad = tgt_tokens.eq(model.vocab["<pad>"]).float().sum().item()
            n_tokens = tgt_tokens.numel() - n_pad - n_sos
            # Keep track
            tot_nll += nll.item()
            tot_tokens += n_tokens
            # for src_line, tgt_line in zip(src_txt, tgt_txt):
            #     in_words = src_line.strip().split()
            #     src_tokens = [model.vocab[word] for word in in_words]
            #     out_tokens = greedy(model, src_tokens)
            #     # Convert back to strings
            #     out_tokens = [model.vocab[tok] for tok in out_tokens]
            #     translated_sentence = desegment(out_tokens)
            #     translated.append(translated_sentence)
            #     ground_truth.append(desegment(tgt_line.strip().split()[1:-1]))
    # bleu = corpus_bleu(translated, [ground_truth])
    # return bleu, exp(tot_nll / tot_tokens)
    return 0, exp(tot_nll / tot_tokens)


def main():
    # Command line arguments
    args = get_args()
    # Set random seed
    th.manual_seed(args.seed)
    # data
    vocab, train_data, valid_data = load_data(args.src, args.tgt)
    # Model
    model = Transformer(
        args.n_layers,
        args.embed_dim,
        args.hidden_dim,
        args.n_heads,
        vocab,
        args.dropout,
        args.word_dropout,
    )
    if args.cuda:
        model = model.cuda()
    # Load existing model
    if os.path.isfile(args.model_file) and not args.overwrite_model:
        model.load_state_dict(th.load(args.model_file))
    # Optimizer
    optim = th.optim.Adam(model.parameters(), lr=args.lr, betas=(0.9, 0.98))
    # Learning rate schedule
    lr_schedule = None
    if args.inverse_sqrt_schedule:
        inverse_sqrt_schedule(2000, args.lr)
    # Dataloader
    train_loader = MTDataLoader(
        train_data,
        max_bsz=args.samples_per_batch,
        max_tokens=args.tokens_per_batch,
        shuffle=True
    )
    valid_loader = MTDataLoader(
        valid_data,
        max_bsz=args.samples_per_batch,
        max_tokens=args.tokens_per_batch,
        shuffle=False
    )
    # Either validate
    if args.validate_only:
        valid_ppl = evaluate_ppl(model, valid_loader)
        print(f"Validation perplexity: {valid_ppl:.2f}")
    else:
        # Train epochs
        best_ppl = 1e12
        criterion = LabelSmoothingLoss(args.label_smoothing, len(vocab), ignore_index=vocab['<pad>'])
        f = open('log.txt', 'w', buffering=1)
        for epoch in range(1, args.n_epochs+1):
            criterion = criterion.cuda()
            print(f"----- Epoch {epoch} -----", flush=True)
            # Train for one epoch
            model.train()
            train_epoch(model, optim, train_loader, criterion,
                        lr_schedule, args.clip_grad)
            # Check dev ppl
            model.eval()
            valid_bleu, valid_ppl = evaluate_ppl(model, valid_loader, epoch)
            if valid_ppl < 9.0:
                criterion = LabelSmoothingLoss(args.label_smoothing - 0.06, len(vocab), ignore_index=vocab['<pad>'])
            elif valid_ppl < 12.0:
                criterion = LabelSmoothingLoss(args.label_smoothing - 0.04, len(vocab), ignore_index=vocab['<pad>'])
            elif valid_ppl < 15.0:
                criterion = LabelSmoothingLoss(args.label_smoothing - 0.02, len(vocab), ignore_index=vocab['<pad>'])
            print(f"Validation perplexity: {valid_ppl:.2f}", flush=True)
            f.write(str(epoch) + "Validation perplexity: " + str(valid_ppl) + "\n")
            # print(f"Validation bleu: {valid_bleu:.4f}", flush=True)
            # Early stopping maybe
            if valid_ppl < best_ppl:
                best_ppl = valid_ppl
                print(f"Saving new best model (epoch {epoch} ppl {valid_ppl})")
                th.save(model.state_dict(), args.model_file)
            else:
                for param_group in optim.param_groups:
                    param_group["lr"] *= args.lr_decay
        f.close()


if __name__ == "__main__":
    main()
