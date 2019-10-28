import torch
import torch.nn.init as init

import numpy as np
import pickle

import os
from glob import glob
from tqdm import tqdm

class TrainLoop(object):

	def __init__(self, model, generator, optimizer, train_loader, valid_loader, checkpoint_path=None, checkpoint_epoch=None, cuda=True, logger=None):
		if checkpoint_path is None:
			# Save to current directory
			self.checkpoint_path = os.getcwd()
		else:
			self.checkpoint_path = checkpoint_path
			if not os.path.isdir(self.checkpoint_path):
				os.mkdir(self.checkpoint_path)

		self.save_epoch_fmt = os.path.join(self.checkpoint_path, 'checkpoint_{}ep.pt')
		self.cuda_mode = cuda
		self.model = model
		self.generator = generator
		self.optimizer = optimizer
		self.train_loader = train_loader
		self.valid_loader = valid_loader
		self.history = {'train_loss': [], 'mse': [], 'intra_mse': [], 'valid_loss': []}
		self.total_iters = 0
		self.cur_epoch = 0
		self.its_without_improv = 0
		self.last_best_val_loss = np.inf
		self.logger = logger

		if checkpoint_epoch is not None:
			self.load_checkpoint(self.save_epoch_fmt.format(checkpoint_epoch))
		else:
			self.initialize_params()

	def train(self, n_epochs=1, save_every=10):

		while self.cur_epoch < n_epochs:
			print('Epoch {}/{}'.format(self.cur_epoch+1, n_epochs))
			train_iter = tqdm(enumerate(self.train_loader))

			train_loss = 0.0
			mse = 0.0
			intra_mse = 0.0
			valid_loss = 0.0

			# Train step

			for t,batch in train_iter:
				new_train_loss = self.train_step(batch)
				train_loss += new_train_loss[0]
				mse += new_train_loss[1]
				intra_mse += new_train_loss[2]
				if self.logger:
					self.logger.add_scalar('Train/Train Loss', new_train_loss[0], self.total_iters)
					self.logger.add_scalar('Train/MSE', new_train_loss[1], self.total_iters)
					self.logger.add_scalar('Train/Intra MSE', new_train_loss[2], self.total_iters)
				self.total_iters += 1

			self.history['train_loss'].append(train_loss/(t+1))
			self.history['mse'].append(mse/(t+1))
			self.history['intra_mse'].append(intra_mse/(t+1))


			# Validation

			for t, batch in enumerate(self.valid_loader):
				new_valid_loss = self.valid(batch)
				valid_loss += new_valid_loss

			self.history['valid_loss'].append(valid_loss/(t+1))

			if self.logger:
				self.logger.add_scalar('Valid/MSE', self.history['valid_loss'][-1], self.total_iters)

			print('Total train loss: {}'.format(self.history['train_loss'][-1]))
			print('Train MSE {}'.format(self.history['mse'][-1]))
			print('Train intra frames MSE {}'.format(self.history['intra_mse'][-1]))
			print('Total valid loss: {}'.format(self.history['valid_loss'][-1]))

			self.cur_epoch += 1

			if valid_loss < self.last_best_val_loss:
				self.its_without_improv = 0
				self.last_best_val_loss = valid_loss
				self.checkpointing()
			else:
				self.its_without_improv += 1
				if self.cur_epoch % save_every == 0:
					self.checkpointing()

		# saving final models
		print('Saving final model...')
		self.checkpointing()

	def train_step(self, batch):

		self.model.train()
		self.optimizer.zero_grad()

		x, y = batch

		if self.cuda_mode:
			x = x.cuda()
			y = y.cuda()

		out = self.model.forward(x)

		loss_overall = 0
		frames_list = []

		for i in range(out.size(1)):

			gen_frame = self.generator(out[:,i,:].squeeze().contiguous())
			frames_list.append(gen_frame.squeeze())
			loss_overall += torch.nn.functional.mse_loss(frames_list[-1], y[:,i,:].squeeze())

		loss_diff = 0
		for i in range(1, out.size(1)):
			loss_diff += torch.nn.functional.mse_loss((frames_list[i]-frames_list[i-1]), (y[:,i,:].squeeze() - y[:,i-1,:].squeeze()))

		loss = loss_diff + loss_overall

		loss.backward()
		self.optimizer.step()

		return loss.item(), loss_overall.item()/(len(frames_list)), loss_diff.item()/(len(frames_list)-1)

	def valid(self, batch):

		self.model.eval()

		x, y = batch

		if self.cuda_mode:
			x = x.cuda()
			y = y.cuda()

		out = self.model.forward(x)

		loss = 0
		frames_list = []

		for i in range(out.size(1)):
			gen_frame = self.generator(out[:,i,:].squeeze().contiguous()).squeeze()
			loss += torch.nn.functional.mse_loss(gen_frame, y[:,i,:].squeeze())
			frames_list.append(gen_frame.unsqueeze(1).unsqueeze(1))

		if self.logger:
			grid = torchvision.utils.make_grid(x)
			self.logger.add_image('Inputs', grid, self.total_iters)
			add_video('Reconstructed', torch.cat(frames_list, 1), self.total_iters)

		return loss.item()/(i+1)

	def checkpointing(self):

		# Checkpointing
		print('Checkpointing...')
		ckpt = {'model_state': self.model.state_dict(),
		'optimizer_state': self.optimizer.state_dict(),
		'history': self.history,
		'total_iters': self.total_iters,
		'cur_epoch': self.cur_epoch,
		'its_without_improve': self.its_without_improv,
		'last_best_val_loss': self.last_best_val_loss}
		torch.save(ckpt, self.save_epoch_fmt.format(self.cur_epoch))

	def load_checkpoint(self, ckpt):

		if os.path.isfile(ckpt):

			ckpt = torch.load(ckpt)
			# Load model state
			self.model.load_state_dict(ckpt['model_state'])
			# Load optimizer state
			self.optimizer.load_state_dict(ckpt['optimizer_state'])
			# Load history
			self.history = ckpt['history']
			self.total_iters = ckpt['total_iters']
			self.cur_epoch = ckpt['cur_epoch']
			self.its_without_improv = ckpt['its_without_improve']
			self.last_best_val_loss = ckpt['last_best_val_loss']

		else:
			print('No checkpoint found at: {}'.format(ckpt))

	def print_params_norms(self):
		norm = 0.0
		for params in list(self.model.parameters()):
			norm+=params.norm(2).item()
		print('Sum of weights norms: {}'.format(norm))


	def print_grad_norms(self):
		norm = 0.0
		for params in list(self.model.parameters()):
			norm+=params.grad.norm(2).item()
		print('Sum of grads norms: {}'.format(norm))

	def initialize_params(self):
		for layer in self.model.modules():
			if isinstance(layer, torch.nn.Conv2d):
				init.kaiming_normal(layer.weight.data)
			elif isinstance(layer, torch.nn.BatchNorm2d):
				layer.weight.data.fill_(1)
				layer.bias.data.zero_()
